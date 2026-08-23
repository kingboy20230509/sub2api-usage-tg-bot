# Sub2API Usage Telegram Bot

A small Telegram bot for checking Sub2API API key usage from Telegram.

一个轻量 Telegram bot，用来通过 Telegram 查询 Sub2API key 的用量。

---

## 中文说明

### 功能

- Telegram 用户发送 `/check`，查询自己绑定的 Sub2API key 用量。
- 支持 Telegram webhook 模式，可配置 webhook secret token。
- 使用 JSON 文件维护「Telegram 用户 ID → Sub2API key 名称」绑定关系。
- 通过 Docker 容器 `sub2api-postgres` 查询 Sub2API 的 PostgreSQL 数据库。
- 无第三方 Python 框架依赖，仅使用 Python 标准库。

### 安全提醒

请不要把真实运行配置提交到 Git 仓库，包括：

- `.env` 或 `/etc/sub2api-tg-bot.env`
- `config.json`
- Telegram Bot Token
- webhook secret
- 真实 Telegram 用户 ID
- 能识别用户或内部账号的真实 key 名称
- 服务器 IP、域名、数据库密码、管理后台地址、运行日志等

本仓库只提供示例配置文件：

- `.env.example`
- `config.example.json`
- `sub2api-tg-bot.service.example`

### 环境要求

- Python 3.10+
- bot 运行用户需要能执行 Docker 命令
- 已运行 Sub2API PostgreSQL 容器，默认容器名为 `sub2api-postgres`
- Sub2API 默认 PostgreSQL 配置：
  - database: `sub2api`
  - user: `sub2api`
- 如果使用 webhook，需要 Nginx/Caddy 等反向代理和 HTTPS 域名

### 文件说明

- `sub2api_tg_bot.py`：bot 主程序
- `config.example.json`：绑定配置示例
- `.env.example`：环境变量示例
- `sub2api-tg-bot.service.example`：systemd 服务示例

### 安装

```bash
sudo mkdir -p /opt/sub2api-tg-bot /etc
sudo cp sub2api_tg_bot.py /opt/sub2api-tg-bot/
sudo cp config.example.json /opt/sub2api-tg-bot/config.json
sudo cp .env.example /etc/sub2api-tg-bot.env
sudo chmod 600 /etc/sub2api-tg-bot.env /opt/sub2api-tg-bot/config.json
```

编辑 `/opt/sub2api-tg-bot/config.json`：

```json
{
  "bindings": {
    "123456789": "example-key-name"
  },
  "timezone": "Asia/Shanghai"
}
```

说明：左边的 `123456789` 是 Telegram 用户 ID 字符串；右边的 `example-key-name` 是 Sub2API 数据库里 `api_keys.name` 的值。

编辑 `/etc/sub2api-tg-bot.env`：

```env
TELEGRAM_BOT_TOKEN=123456789:replace_me
WEBHOOK_SECRET=replace_me_with_a_long_random_string
PUBLIC_WEBHOOK_URL=https://example.com/tg-sub2api-bot/replace_me
WEBHOOK_PATH=/tg-sub2api-bot/replace_me
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8099
SUB2API_TG_BOT_CONFIG=/opt/sub2api-tg-bot/config.json
```

### Nginx webhook 示例

```nginx
location /tg-sub2api-bot/replace_me {
    proxy_pass http://127.0.0.1:8099/tg-sub2api-bot/replace_me;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### systemd 启动

```bash
sudo cp sub2api-tg-bot.service.example /etc/systemd/system/sub2api-tg-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now sub2api-tg-bot.service
sudo journalctl -u sub2api-tg-bot.service -f
```

### 使用

在 Telegram 里给 bot 发送：

- `/start`：查看提示
- `/check`：查询当前 Telegram 用户绑定的 Sub2API key 用量

### 排查

查看服务状态和日志：

```bash
systemctl status sub2api-tg-bot.service
journalctl -u sub2api-tg-bot.service -n 100 --no-pager
```

检查本地 health endpoint：

```bash
curl http://127.0.0.1:8099/health
```

检查 Telegram webhook 状态：

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

---

## English

## Features

- `/check` returns usage for the Telegram user’s bound Sub2API key.
- Webhook mode with optional Telegram webhook secret token.
- Reads bindings from a JSON config file.
- Queries the Sub2API PostgreSQL database through the `sub2api-postgres` Docker container.
- No framework dependency; uses Python standard library only.

## Security notes

Do **not** commit real runtime files:

- `.env` or `/etc/sub2api-tg-bot.env`
- `config.json`
- Telegram bot tokens
- webhook secrets
- real Telegram user IDs if you consider them private
- real key names if they identify users or internal accounts
- server IPs, domains, database credentials, or logs

This repository includes only example config files.

## Requirements

- Python 3.10+
- Docker access from the bot process
- A running Sub2API PostgreSQL container named `sub2api-postgres`
- PostgreSQL database/user defaults used by Sub2API:
  - database: `sub2api`
  - user: `sub2api`
- A reverse proxy such as Nginx/Caddy if using Telegram webhook mode

## Files

- `sub2api_tg_bot.py` — bot program
- `config.example.json` — example Telegram user ID to Sub2API key-name bindings
- `.env.example` — example environment variables
- `sub2api-tg-bot.service.example` — example systemd unit

## Configuration

Copy examples and edit them for your environment:

```bash
sudo mkdir -p /opt/sub2api-tg-bot /etc
sudo cp sub2api_tg_bot.py /opt/sub2api-tg-bot/
sudo cp config.example.json /opt/sub2api-tg-bot/config.json
sudo cp .env.example /etc/sub2api-tg-bot.env
sudo chmod 600 /etc/sub2api-tg-bot.env /opt/sub2api-tg-bot/config.json
```

Example `config.json`:

```json
{
  "bindings": {
    "123456789": "example-key-name"
  },
  "timezone": "Asia/Shanghai"
}
```

The key on the left is the Telegram user ID as a string. The value is the `api_keys.name` value in Sub2API.

Example environment:

```env
TELEGRAM_BOT_TOKEN=123456789:replace_me
WEBHOOK_SECRET=replace_me_with_a_long_random_string
PUBLIC_WEBHOOK_URL=https://example.com/tg-sub2api-bot/replace_me
WEBHOOK_PATH=/tg-sub2api-bot/replace_me
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8099
SUB2API_TG_BOT_CONFIG=/opt/sub2api-tg-bot/config.json
```

## Nginx webhook example

```nginx
location /tg-sub2api-bot/replace_me {
    proxy_pass http://127.0.0.1:8099/tg-sub2api-bot/replace_me;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## systemd

```bash
sudo cp sub2api-tg-bot.service.example /etc/systemd/system/sub2api-tg-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now sub2api-tg-bot.service
sudo journalctl -u sub2api-tg-bot.service -f
```

## Usage

Send `/start` or `/check` to the bot.

## Troubleshooting

Check service status and logs:

```bash
systemctl status sub2api-tg-bot.service
journalctl -u sub2api-tg-bot.service -n 100 --no-pager
```

Check local health endpoint:

```bash
curl http://127.0.0.1:8099/health
```

Check Telegram webhook state:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

## License

MIT
