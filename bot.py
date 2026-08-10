"""Telegram video -> local NAS folder bot."""
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "").rstrip("/")
SAVE_SUBDIR = os.environ.get("SAVE_SUBDIR", "Telegram Videos").strip("/")
ALLOWED_IDS = {int(item) for item in os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "").split(",") if item.strip()}
SAVE_DIR = Path("/data") / SAVE_SUBDIR


def safe_name(original: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|#%]', "_", Path(original).stem).strip() or "video"
    suffix = re.sub(r"[^.A-Za-z0-9]", "", Path(original).suffix) or ".mp4"
    return f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"


def is_authorized(update: Update) -> bool:
    return bool(update.effective_user and (not ALLOWED_IDS or update.effective_user.id in ALLOWED_IDS))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.effective_message.reply_text("你没有权限使用这个机器人。")
        return
    await update.effective_message.reply_text("把视频发给我，我会保存到 NAS。")


async def save_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        await update.effective_message.reply_text("你没有权限使用这个机器人。")
        return
    message = update.effective_message
    attachment = message.video or message.document
    if not attachment or (message.document and not (message.document.mime_type or "").startswith("video/")):
        return

    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    original_name = getattr(attachment, "file_name", None) or "telegram_video.mp4"
    destination = SAVE_DIR / safe_name(original_name)
    status = await message.reply_text("正在保存到 NAS…")
    try:
        file = await context.bot.get_file(attachment.file_id)
        await file.download_to_drive(custom_path=destination)
        await status.edit_text(f"已保存：{destination.name}")
        LOG.info("Saved %s", destination)
    except Exception:
        LOG.exception("Could not save Telegram video")
        destination.unlink(missing_ok=True)
        await status.edit_text("保存失败，请查看 NAS 容器日志。")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("请先在 .env 中设置 TELEGRAM_BOT_TOKEN")
    builder = Application.builder().token(BOT_TOKEN)
    if LOCAL_BOT_API_URL:
        # Local mode has no cloud Bot API download cap.  Both containers share
        # the Local Bot API data volume, so returned local file paths are valid.
        builder = builder.base_url(f"{LOCAL_BOT_API_URL}/bot").base_file_url(f"{LOCAL_BOT_API_URL}/file/bot").local_mode(True)
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler((filters.VIDEO | filters.Document.VIDEO) & ~filters.COMMAND, save_video))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
