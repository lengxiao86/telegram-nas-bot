#!/usr/bin/env bash
# One-command installer for a Feiniu NAS Docker host.
set -euo pipefail

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose is not available. Please install/enable Docker in fnOS first."
  exit 1
fi

if [ -f .env ]; then
  echo ".env already exists; it was not overwritten."
  echo "To reconfigure, edit .env and run: docker compose up -d --build"
  exit 0
fi

echo "Telegram -> Feiniu NAS large-video bot setup"
echo "Get a bot token from @BotFather, and api_id/api_hash from https://my.telegram.org/apps"
read -r -p "BotFather bot token: " bot_token
read -r -p "Telegram api_id: " api_id
read -r -p "Telegram api_hash: " api_hash
read -r -p "NAS shared-folder absolute path (for example /vol1/1000/TelegramVideos): " video_dir
read -r -p "Your Telegram numeric ID (optional but recommended): " allowed_id

if [ -z "$bot_token" ] || [ -z "$api_id" ] || [ -z "$api_hash" ] || [ -z "$video_dir" ]; then
  echo "Bot token, api_id, api_hash, and NAS folder path are all required."
  exit 1
fi

mkdir -p "$video_dir"
if [ ! -w "$video_dir" ]; then
  echo "Cannot write to $video_dir. Choose a writable NAS shared-folder path."
  exit 1
fi

umask 077
{
  printf 'TELEGRAM_BOT_TOKEN=%s\n' "$bot_token"
  printf 'TELEGRAM_API_ID=%s\n' "$api_id"
  printf 'TELEGRAM_API_HASH=%s\n' "$api_hash"
  printf 'NAS_VIDEO_DIR=%s\n' "$video_dir"
  printf 'ALLOWED_TELEGRAM_USER_IDS=%s\n' "$allowed_id"
  printf 'SAVE_SUBDIR=\n'
  printf 'MAX_CONCURRENT_TRANSFERS=2\n'
} > .env

echo "Building Telegram Local Bot API Server. First build can take several minutes…"
docker compose up -d --build
echo
echo "Done. Send a video to your bot; it will be saved under:"
echo "$video_dir"
echo "Logs: docker compose logs -f"
