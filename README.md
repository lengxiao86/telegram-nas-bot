# 飞牛 NAS：Telegram 视频自动保存

这是适合大视频的方案：Telegram 机器人和 **Telegram Local Bot API Server** 都直接运行在飞牛 NAS 的 Docker 中，视频直接落到 NAS 共享文件夹。不需要 OneDrive、Azure 或任何微软授权。Local Bot API Server 消除了官方云端 Bot API 的 20 MB 下载限制。

## 一键部署（推荐）

1. 在飞牛 NAS 的文件管理中创建共享文件夹，例如 `TelegramVideos`。
2. 用 Telegram 的 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建机器人，复制 token。
3. 打开 [my.telegram.org/apps](https://my.telegram.org/apps)，以同一个 Telegram 帐户登录，创建一个 API application，复制 `api_id` 和 `api_hash`。
4. 将本项目复制到 NAS 的一个文件夹，例如 `/vol1/1000/docker/telegram-nas-bot`，在该文件夹的终端运行：

   ```bash
   chmod +x setup.sh && ./setup.sh
   ```

按提示粘贴 token、`api_id`、`api_hash`、NAS 文件夹绝对路径和你的 Telegram 数字 ID 即可。脚本会自动生成 `.env`、编译服务并启动机器人。

## 手动部署

若不使用安装脚本，可按以下步骤配置：

1. 打开飞牛 NAS 的 **文件管理**，创建共享文件夹，例如 `TelegramVideos`。
2. 用 Telegram 的 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 创建机器人，复制 token。
3. 打开 [my.telegram.org/apps](https://my.telegram.org/apps)，以同一个 Telegram 帐户登录，创建一个 API application，复制 `api_id` 和 `api_hash`。这是 Local Bot API Server 的必要配置，**不是** BotFather token。
4. 将本项目复制到 NAS 的一个文件夹（例如 `/vol1/1000/docker/telegram-nas-bot`）。
5. 复制并编辑配置：

   ```bash
   cp .env.example .env
   ```

   在 `.env` 填写 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_API_ID` 和 `TELEGRAM_API_HASH`。强烈建议填写 `ALLOWED_TELEGRAM_USER_IDS`，你的数字 ID 可从 [@userinfobot](https://t.me/userinfobot) 获取。
6. 在 `.env` 设置 `NAS_VIDEO_DIR` 为你刚创建的共享文件夹实际绝对路径。
7. 在该项目目录打开飞牛 NAS 的终端，运行：

   ```bash
   docker compose up -d --build
   ```

首次构建会从 Telegram 官方开源仓库编译 Local Bot API Server，耗时可能较长、需要数 GB 可用空间；完成一次后，后续启动很快。用下面命令查看运行日志：

```bash
docker compose logs -f
```

停止机器人：

```bash
docker compose down
```

## 使用

向机器人发送视频（或以文件形式发送视频），它会回复「已保存」，文件会出现在 NAS 挂载共享目录下的 `Telegram Videos` 子文件夹中。文件名自动加入时间，避免覆盖。

## 大视频说明

- 本项目已启用 Local Bot API Server，因此不会受官方云端 Bot API 的 **20 MB 下载限制**影响。
- 单个视频仍受 Telegram 本身允许发送的文件大小限制；请以你 Telegram 客户端实际可发送的最大文件为准。
- NAS 必须有足够的空间容纳「原视频 + Local Bot API 临时文件」。确认上传完成后，原视频会保存在共享目录；运行中的临时缓存由 Local Bot API Server 管理。
