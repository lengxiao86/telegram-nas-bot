"""Telegram video -> local NAS folder bot."""
import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").rstrip("/")
# Empty by default: save straight into the NAS directory selected at setup.
SAVE_SUBDIR = os.environ.get("SAVE_SUBDIR", "").strip("/")
MAX_CONCURRENT_TRANSFERS = max(1, int(os.environ.get("MAX_CONCURRENT_TRANSFERS", "2")))
REQUEST_READ_TIMEOUT = float(os.environ.get("TELEGRAM_REQUEST_READ_TIMEOUT", "900"))
DOWNLOAD_RETRIES = max(1, int(os.environ.get("DOWNLOAD_RETRIES", "3")))
RETRY_DELAY_SECONDS = max(1, int(os.environ.get("RETRY_DELAY_SECONDS", "15")))
CACHE_RETENTION_HOURS = max(0, int(os.environ.get("CACHE_RETENTION_HOURS", "168")))
CACHE_CLEAN_INTERVAL_MINUTES = max(1, int(os.environ.get("CACHE_CLEAN_INTERVAL_MINUTES", "60")))
ALLOWED_IDS = {int(item) for item in os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(",") if item.strip()}
SAVE_DIR = Path("/data") / SAVE_SUBDIR
CACHE_DIR = Path("/var/lib/telegram-bot-api")
STATE_DB = SAVE_DIR / ".telegram-nas-bot.db"
MEDIA_SUFFIXES = {".3gp", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm", ".wmv"}


def safe_name(original: str, message_id: int) -> str:
    stem = re.sub(r'[\\/:*?"<>|#%]', "_", Path(original).stem).strip() or "video"
    suffix = re.sub(r"[^.A-Za-z0-9]", "", Path(original).suffix) or ".mp4"
    return f"{stem}_{message_id}_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"


def readable_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} TB"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class StateStore:
    """Small persistent index for duplicate checks and recent-file commands."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS saved_files (
                id INTEGER PRIMARY KEY,
                telegram_unique_id TEXT UNIQUE,
                sha256 TEXT UNIQUE,
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                saved_at REAL NOT NULL
            )"""
        )
        self.connection.commit()

    def find_duplicate(self, unique_id: Optional[str] = None, sha256: Optional[str] = None) -> Optional[sqlite3.Row]:
        if not unique_id and not sha256:
            return None
        row = self.connection.execute(
            "SELECT * FROM saved_files WHERE telegram_unique_id = ? OR sha256 = ? LIMIT 1",
            (unique_id, sha256),
        ).fetchone()
        if row and not Path(row["path"]).is_file():
            self.connection.execute("DELETE FROM saved_files WHERE id = ?", (row["id"],))
            self.connection.commit()
            return None
        return row

    def save(self, unique_id: str, sha256: str, path: Path) -> Optional[sqlite3.Row]:
        duplicate = self.find_duplicate(unique_id, sha256)
        if duplicate:
            return duplicate
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO saved_files (telegram_unique_id, sha256, path, size, saved_at) VALUES (?, ?, ?, ?, ?)",
            (unique_id, sha256, str(path), path.stat().st_size, time.time()),
        )
        self.connection.commit()
        return self.find_duplicate(unique_id, sha256) if cursor.rowcount == 0 else None

    def recent(self, limit: int) -> list[sqlite3.Row]:
        rows = self.connection.execute("SELECT * FROM saved_files ORDER BY saved_at DESC LIMIT ?", (limit,)).fetchall()
        return [row for row in rows if Path(row["path"]).is_file()]


class TransferTracker:
    def __init__(self) -> None:
        self.waiting = 0
        self.active = 0
        self.lock = asyncio.Lock()

    async def enqueue(self) -> None:
        async with self.lock:
            self.waiting += 1

    async def start(self) -> None:
        async with self.lock:
            self.waiting -= 1
            self.active += 1

    async def cancel_waiting(self) -> None:
        async with self.lock:
            self.waiting = max(0, self.waiting - 1)

    async def finish(self) -> None:
        async with self.lock:
            self.active = max(0, self.active - 1)

    async def snapshot(self) -> tuple[int, int]:
        async with self.lock:
            return self.waiting, self.active


def is_authorized(update: Update) -> bool:
    return bool(update.effective_user and (not ALLOWED_IDS or update.effective_user.id in ALLOWED_IDS))


async def deny_if_needed(update: Update) -> bool:
    if is_authorized(update):
        return False
    await update.effective_message.reply_text("你没有权限使用这个机器人。")
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    await update.effective_message.reply_text("把视频发给我，我会保存到 NAS。可用命令：/status、/recent")


def recent_lines(store: StateStore, limit: int) -> list[str]:
    rows = store.recent(limit)
    if not rows:
        return ["暂无已保存的视频。"]
    return [
        f"{index}. {Path(row['path']).name} · {readable_size(row['size'])} · {datetime.fromtimestamp(row['saved_at']).strftime('%m-%d %H:%M')}"
        for index, row in enumerate(rows, 1)
    ]


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    free = os.statvfs(SAVE_DIR).f_bavail * os.statvfs(SAVE_DIR).f_frsize
    waiting, active = await context.application.bot_data["tracker"].snapshot()
    recent = recent_lines(context.application.bot_data["store"], 3)
    await update.effective_message.reply_text(
        "NAS 状态\n"
        f"剩余空间：{readable_size(free)}\n"
        f"传输中：{active} · 排队中：{waiting}\n"
        "最近保存：\n" + "\n".join(recent)
    )


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    await update.effective_message.reply_text("最近保存的 10 个文件：\n" + "\n".join(recent_lines(context.application.bot_data["store"], 10)))


async def download_with_retries(context: ContextTypes.DEFAULT_TYPE, attachment, destination: Path, status_message) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            destination.unlink(missing_ok=True)
            telegram_file = await context.bot.get_file(attachment.file_id)
            await telegram_file.download_to_drive(custom_path=destination)
            return
        except Exception as error:  # Network/API failures are expected for large files.
            last_error = error
            destination.unlink(missing_ok=True)
            if attempt < DOWNLOAD_RETRIES:
                await status_message.edit_text(f"保存遇到网络问题，{RETRY_DELAY_SECONDS} 秒后重试（{attempt + 1}/{DOWNLOAD_RETRIES}）…")
                await asyncio.sleep(RETRY_DELAY_SECONDS * attempt)
    assert last_error is not None
    raise last_error


async def save_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    message = update.effective_message
    attachment = message.video or message.document
    if not attachment or (message.document and not (message.document.mime_type or "").startswith("video/")):
        return

    store: StateStore = context.application.bot_data["store"]
    existing = store.find_duplicate(unique_id=attachment.file_unique_id)
    if existing:
        await message.reply_text(f"该视频已保存过，无需重复下载：{Path(existing['path']).name}")
        return

    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    destination = SAVE_DIR / safe_name(getattr(attachment, "file_name", None) or "telegram_video.mp4", message.message_id)
    status_message = await message.reply_text("已接收，正在排队保存到 NAS…")
    tracker: TransferTracker = context.application.bot_data["tracker"]
    await tracker.enqueue()
    started = False
    try:
        semaphore = context.application.bot_data["transfer_semaphore"]
        async with semaphore:
            await tracker.start()
            started = True
            await status_message.edit_text("正在保存到 NAS…")
            await download_with_retries(context, attachment, destination, status_message)
            digest = await asyncio.to_thread(file_hash, destination)
            duplicate = store.save(attachment.file_unique_id, digest, destination)
            if duplicate:
                destination.unlink(missing_ok=True)
                await status_message.edit_text(f"该视频已存在，未重复保存：{Path(duplicate['path']).name}")
                return
        await status_message.edit_text(f"已保存：{destination.name}")
        LOG.info("Saved %s", destination)
    except Exception as error:
        LOG.exception("Could not save Telegram video")
        destination.unlink(missing_ok=True)
        detail = "网络请求超时" if isinstance(error, TimedOut) else "下载或写入失败"
        await status_message.edit_text(f"保存失败：{detail}。已自动重试 {DOWNLOAD_RETRIES} 次，请稍后重新发送。")
    finally:
        if started:
            await tracker.finish()
        else:
            await tracker.cancel_waiting()


def clean_cache() -> tuple[int, int]:
    if CACHE_RETENTION_HOURS == 0 or not CACHE_DIR.exists():
        return 0, 0
    cutoff = time.time() - CACHE_RETENTION_HOURS * 3600
    deleted = reclaimed = 0
    for file in CACHE_DIR.rglob("*"):
        if file.is_symlink() or not file.is_file() or file.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        with suppress(OSError):
            if file.stat().st_mtime < cutoff:
                reclaimed += file.stat().st_size
                file.unlink()
                deleted += 1
    return deleted, reclaimed


async def cache_cleaner(application: Application) -> None:
    while True:
        try:
            deleted, reclaimed = await asyncio.to_thread(clean_cache)
            if deleted:
                LOG.info("Cleaned %d cached video file(s), recovered %s", deleted, readable_size(reclaimed))
        except Exception:
            LOG.exception("Cache cleanup failed")
        await asyncio.sleep(CACHE_CLEAN_INTERVAL_MINUTES * 60)


async def on_startup(application: Application) -> None:
    deleted, reclaimed = await asyncio.to_thread(clean_cache)
    if deleted:
        LOG.info("Startup cleanup recovered %s", readable_size(reclaimed))
    application.bot_data["cache_task"] = asyncio.create_task(cache_cleaner(application))


async def on_shutdown(application: Application) -> None:
    task = application.bot_data.get("cache_task")
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("请先在 .env 中设置 TELEGRAM_BOT_TOKEN")
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(16)
        .connect_timeout(30)
        .read_timeout(REQUEST_READ_TIMEOUT)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
    )
    if LOCAL_BOT_API_URL:
        builder = builder.base_url(f"{LOCAL_BOT_API_URL}/bot").base_file_url(f"{LOCAL_BOT_API_URL}/file/bot").local_mode(True)
    app = builder.build()
    app.bot_data["store"] = StateStore(STATE_DB)
    app.bot_data["tracker"] = TransferTracker()
    app.bot_data["transfer_semaphore"] = asyncio.Semaphore(MAX_CONCURRENT_TRANSFERS)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(MessageHandler((filters.VIDEO | filters.Document.VIDEO) & ~filters.COMMAND, save_video))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
