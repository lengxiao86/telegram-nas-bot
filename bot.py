"""Telegram video -> local NAS folder bot."""
import asyncio
import hashlib
import io
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
PROGRESS_UPDATE_SECONDS = max(1.0, float(os.environ.get("PROGRESS_UPDATE_SECONDS", "2")))
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

    def forget_path(self, path: Path) -> None:
        """Remove an index entry for a file that was deliberately deleted."""
        self.connection.execute("DELETE FROM saved_files WHERE path = ?", (str(path),))
        self.connection.commit()


class TransferTracker:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, int | bool]] = {}
        self.lock = asyncio.Lock()

    async def enqueue(self, task_id: str, total: int) -> None:
        async with self.lock:
            self.tasks[task_id] = {"total": max(0, total), "completed": 0, "active": False}

    async def start(self, task_id: str) -> None:
        async with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id]["active"] = True

    async def progress(self, task_id: str, completed: int, total: int) -> dict[str, int]:
        async with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id]["total"] = max(0, total)
                self.tasks[task_id]["completed"] = max(0, min(completed, total))
            return self._snapshot()

    async def remove(self, task_id: str) -> None:
        async with self.lock:
            self.tasks.pop(task_id, None)

    def _snapshot(self) -> dict[str, int]:
        waiting = sum(1 for task in self.tasks.values() if not task["active"])
        active = sum(1 for task in self.tasks.values() if task["active"])
        total = sum(int(task["total"]) for task in self.tasks.values())
        completed = sum(int(task["completed"]) for task in self.tasks.values())
        return {"waiting": waiting, "active": active, "total": total, "completed": completed}

    async def snapshot(self) -> dict[str, int]:
        async with self.lock:
            return self._snapshot()


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
    await update.effective_message.reply_text(
        "把视频发给我，我会保存到 NAS。可用命令：/status、/recent、/dedupe_report、/dedupe_delete"
    )


def recent_lines(store: StateStore, limit: int) -> list[str]:
    rows = store.recent(limit)
    if not rows:
        return ["暂无已保存的视频。"]
    return [
        f"{index}. {Path(row['path']).name} · {readable_size(row['size'])} · {datetime.fromtimestamp(row['saved_at']).strftime('%m-%d %H:%M')}"
        for index, row in enumerate(rows, 1)
    ]


def scan_duplicate_videos(root: Path) -> tuple[int, int, list[tuple[str, list[Path]]]]:
    """Return video count, reclaimable bytes, and exact-duplicate file groups."""
    by_size: dict[int, list[Path]] = {}
    video_count = 0
    for directory, _, names in os.walk(root):
        for name in names:
            candidate = Path(directory) / name
            if candidate.is_symlink() or candidate.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            with suppress(OSError):
                by_size.setdefault(candidate.stat().st_size, []).append(candidate)
                video_count += 1

    by_hash: dict[str, list[Path]] = {}
    for candidates in by_size.values():
        if len(candidates) < 2:
            continue
        for candidate in candidates:
            with suppress(OSError):
                by_hash.setdefault(file_hash(candidate), []).append(candidate)

    groups = [
        (digest, sorted(group, key=lambda path: (path.stat().st_mtime, str(path))))
        for digest, group in by_hash.items()
        if len(group) > 1
    ]
    groups.sort(key=lambda item: item[1][0].stat().st_size * (len(item[1]) - 1), reverse=True)
    reclaimable = sum(group[0].stat().st_size * (len(group) - 1) for _, group in groups)
    return video_count, reclaimable, groups


def duplicate_report_text(video_count: int, reclaimable: int, groups: list[tuple[str, list[Path]]]) -> str:
    lines = [
        "Telegram NAS Bot - 精确重复视频报告",
        f"扫描目录：{SAVE_DIR}",
        f"扫描视频：{video_count}",
        f"重复组数：{len(groups)}",
        f"可释放空间：{readable_size(reclaimable)}",
        "",
        "说明：依据 SHA-256 文件内容检测；本报告不会移动或删除文件。",
    ]
    for number, (_, group) in enumerate(groups, 1):
        original = group[0].relative_to(SAVE_DIR)
        size = readable_size(group[0].stat().st_size)
        lines.extend(["", f"重复组 {number}（每个 {size}）", f"保留建议：{original}"])
        lines.extend(f"重复副本：{path.relative_to(SAVE_DIR)}" for path in group[1:])
    return "\n".join(lines) + "\n"


def make_dedupe_plan(groups: list[tuple[str, list[Path]]]) -> list[dict[str, object]]:
    """Keep enough immutable information to validate files again before deletion."""
    return [
        {
            "sha256": digest,
            "size": group[0].stat().st_size,
            "original": str(group[0]),
            "duplicates": [str(path) for path in group[1:]],
        }
        for digest, group in groups
    ]


def is_in_save_dir(path: Path) -> bool:
    try:
        path.resolve().relative_to(SAVE_DIR.resolve())
        return True
    except ValueError:
        return False


def duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {seconds} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def queue_text(snapshot: dict[str, int]) -> str:
    line = f"队列：传输中 {snapshot['active']} · 等待 {snapshot['waiting']}"
    if snapshot["total"] > 0:
        percent = snapshot["completed"] * 100 / snapshot["total"]
        line += f"\n总进度：{percent:.1f}%（{readable_size(snapshot['completed'])}/{readable_size(snapshot['total'])}）"
    return line


def transfer_progress_text(filename: str, completed: int, total: int, speed: float, snapshot: dict[str, int]) -> str:
    percent = completed * 100 / total if total else 0
    remaining = (total - completed) / speed if speed > 0 else 0
    return (
        f"正在写入 NAS：{filename}\n"
        f"本视频：{percent:.1f}%（{readable_size(completed)}/{readable_size(total)}）\n"
        f"速度：{readable_size(int(speed))}/s · 预计剩余：{duration_text(remaining)}\n"
        + queue_text(snapshot)
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    free = os.statvfs(SAVE_DIR).f_bavail * os.statvfs(SAVE_DIR).f_frsize
    queue = await context.application.bot_data["tracker"].snapshot()
    recent = recent_lines(context.application.bot_data["store"], 3)
    await update.effective_message.reply_text(
        "NAS 状态\n"
        f"剩余空间：{readable_size(free)}\n"
        + queue_text(queue) + "\n"
        "最近保存：\n" + "\n".join(recent)
    )


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    await update.effective_message.reply_text("最近保存的 10 个文件：\n" + "\n".join(recent_lines(context.application.bot_data["store"], 10)))


async def dedupe_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    lock: asyncio.Lock = context.application.bot_data["dedupe_lock"]
    if lock.locked():
        await update.effective_message.reply_text("已有去重扫描正在执行，请等待其完成。")
        return
    async with lock:
        progress = await update.effective_message.reply_text("正在扫描 NAS 文件夹中的视频并计算精确指纹，视频较多时可能需要几分钟…")
        try:
            video_count, reclaimable, groups = await asyncio.to_thread(scan_duplicate_videos, SAVE_DIR)
            context.application.bot_data["dedupe_plan"] = {
                "created_at": time.time(),
                "video_count": video_count,
                "reclaimable": reclaimable,
                "groups": make_dedupe_plan(groups),
            }
            report = duplicate_report_text(video_count, reclaimable, groups)
            await progress.edit_text(
                f"扫描完成：共 {video_count} 个视频，发现 {len(groups)} 组精确重复文件，可释放 {readable_size(reclaimable)}。"
            )
            if groups:
                payload = io.BytesIO(report.encode("utf-8"))
                payload.name = f"dedupe-report-{time.strftime('%Y%m%d-%H%M%S')}.txt"
                await update.effective_message.reply_document(
                    document=payload,
                    caption="这是只读报告：未移动或删除任何文件。",
                )
        except Exception:
            LOG.exception("Duplicate scan failed")
            await progress.edit_text("去重扫描失败。请查看 NAS 容器日志，并确认保存文件夹可读。")


async def dedupe_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask for an explicit Telegram confirmation before a permanent delete."""
    if await deny_if_needed(update):
        return
    plan = context.application.bot_data.get("dedupe_plan")
    queue = await context.application.bot_data["tracker"].snapshot()
    if queue["active"] or queue["waiting"]:
        await update.effective_message.reply_text("当前仍有视频在传输或排队。请完成后再执行删除，避免扫描结果过期。")
        return
    if not plan or time.time() - plan["created_at"] > 60 * 60:
        await update.effective_message.reply_text("请先运行 /dedupe_report 完成一次新的扫描；扫描结果仅在 1 小时内可用于删除。")
        return
    copies = sum(len(group["duplicates"]) for group in plan["groups"])
    if not copies:
        await update.effective_message.reply_text("最近一次扫描没有发现可删除的精确重复视频。")
        return
    context.application.bot_data["dedupe_delete_confirmation_until"] = time.time() + 10 * 60
    await update.effective_message.reply_text(
        f"准备永久删除 {copies} 个精确重复副本，预计释放 {readable_size(plan['reclaimable'])}。\n"
        "每组会保留最早保存的一份；删除前会再次核对 SHA-256。\n\n"
        "如确认，请在 10 分钟内发送：/dedupe_delete_confirm"
    )


async def dedupe_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await deny_if_needed(update):
        return
    plan = context.application.bot_data.get("dedupe_plan")
    confirmation_until = context.application.bot_data.get("dedupe_delete_confirmation_until", 0)
    queue = await context.application.bot_data["tracker"].snapshot()
    if queue["active"] or queue["waiting"]:
        await update.effective_message.reply_text("当前仍有视频在传输或排队，已取消本次删除确认。")
        context.application.bot_data.pop("dedupe_delete_confirmation_until", None)
        return
    if not plan or time.time() - plan["created_at"] > 60 * 60 or time.time() > confirmation_until:
        await update.effective_message.reply_text("删除确认已过期。请重新运行 /dedupe_report，然后发送 /dedupe_delete。")
        return

    status_message = await update.effective_message.reply_text("正在再次核对文件内容，并永久删除重复副本…")
    store: StateStore = context.application.bot_data["store"]
    deleted = 0
    released = 0
    skipped = 0
    try:
        for group in plan["groups"]:
            original = Path(group["original"])
            expected_hash = group["sha256"]
            expected_size = group["size"]
            if not (is_in_save_dir(original) and original.is_file() and not original.is_symlink() and original.stat().st_size == expected_size):
                skipped += len(group["duplicates"])
                continue
            if await asyncio.to_thread(file_hash, original) != expected_hash:
                skipped += len(group["duplicates"])
                continue
            for raw_path in group["duplicates"]:
                candidate = Path(raw_path)
                if not (is_in_save_dir(candidate) and candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size == expected_size):
                    skipped += 1
                    continue
                if await asyncio.to_thread(file_hash, candidate) != expected_hash:
                    skipped += 1
                    continue
                size = candidate.stat().st_size
                await asyncio.to_thread(candidate.unlink)
                store.forget_path(candidate)
                deleted += 1
                released += size
        await status_message.edit_text(f"去重完成：已永久删除 {deleted} 个重复视频，释放 {readable_size(released)}。跳过 {skipped} 个未能再次验证的文件。")
    except Exception:
        LOG.exception("Duplicate deletion failed")
        await status_message.edit_text(f"删除过程中发生错误：已删除 {deleted} 个文件，释放 {readable_size(released)}。请查看 NAS 容器日志。")
    finally:
        context.application.bot_data.pop("dedupe_delete_confirmation_until", None)
        context.application.bot_data.pop("dedupe_plan", None)


def copy_chunk(source, target) -> int:
    block = source.read(4 * 1024 * 1024)
    if block:
        target.write(block)
    return len(block)


async def copy_local_file_with_progress(source_path: Path, destination: Path, task_id: str, filename: str, tracker: TransferTracker, status_message) -> None:
    total = source_path.stat().st_size
    completed = 0
    started_at = last_at = time.monotonic()
    last_completed = 0
    with source_path.open("rb") as source, destination.open("wb") as target:
        while True:
            length = await asyncio.to_thread(copy_chunk, source, target)
            if not length:
                break
            completed += length
            now = time.monotonic()
            if completed == total or now - last_at >= PROGRESS_UPDATE_SECONDS:
                speed = (completed - last_completed) / max(now - last_at, 0.01)
                snapshot = await tracker.progress(task_id, completed, total)
                await status_message.edit_text(transfer_progress_text(filename, completed, total, speed, snapshot))
                last_at, last_completed = now, completed
        await asyncio.to_thread(target.flush)
    # Ensure a final 100% update if the file size changed while it was copied.
    if completed != last_completed:
        snapshot = await tracker.progress(task_id, completed, total)
        speed = completed / max(time.monotonic() - started_at, 0.01)
        await status_message.edit_text(transfer_progress_text(filename, completed, total, speed, snapshot))


async def download_with_retries(
    context: ContextTypes.DEFAULT_TYPE,
    attachment,
    destination: Path,
    task_id: str,
    filename: str,
    tracker: TransferTracker,
    status_message,
) -> None:
    last_error: Optional[Exception] = None
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            destination.unlink(missing_ok=True)
            snapshot = await tracker.snapshot()
            await status_message.edit_text("正在从 Telegram 获取视频文件…\n" + queue_text(snapshot))
            telegram_file = await context.bot.get_file(attachment.file_id)
            source_path = Path(telegram_file.file_path) if LOCAL_BOT_API_URL and telegram_file.file_path else None
            if source_path and source_path.is_file():
                await copy_local_file_with_progress(source_path, destination, task_id, filename, tracker, status_message)
            else:
                # Non-local fallback: Telegram's remote API does not expose stream progress.
                await telegram_file.download_to_drive(custom_path=destination)
                await tracker.progress(task_id, destination.stat().st_size, destination.stat().st_size)
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
    task_id = f"{message.chat_id}:{message.message_id}"
    filename = destination.name
    await tracker.enqueue(task_id, getattr(attachment, "file_size", 0) or 0)
    started = False
    try:
        semaphore = context.application.bot_data["transfer_semaphore"]
        async with semaphore:
            await tracker.start(task_id)
            started = True
            await download_with_retries(context, attachment, destination, task_id, filename, tracker, status_message)
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
        await tracker.remove(task_id)


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
    app.bot_data["dedupe_lock"] = asyncio.Lock()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("dedupe_report", dedupe_report))
    app.add_handler(CommandHandler("dedupe_delete", dedupe_delete))
    app.add_handler(CommandHandler("dedupe_delete_confirm", dedupe_delete_confirm))
    app.add_handler(MessageHandler((filters.VIDEO | filters.Document.VIDEO) & ~filters.COMMAND, save_video))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
