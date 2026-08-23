# Sub2API Usage Telegram Bot

A small Telegram bot for checking Sub2API API key usage from Telegram.

一个轻量 Telegram bot，用来通过 Telegram 查询 Sub2API key 的用量。

---

## 中文说明

### 功能

- Telegram 用户发送 `/check`，查询自己绑定的 Sub2API key 用量。
- 同时显示 5 小时、日、周限额，以及对应已用量和剩余额度；未设置的限制显示为“不限”。
- 后台定时检查周限额，剩余不超过 20% 时主动通知绑定的 Telegram 用户；同一周窗口只提醒一次。
- 使用强制 Telegram webhook secret token，并限制请求体、并发和查询频率。
- 仅允许绑定用户通过私聊查询；群聊不会返回用量数据。
- 使用 JSON 文件维护「Telegram 用户 ID → Sub2API key 名称」绑定关系。
- 使用独立 PostgreSQL 登录，只能执行仓库提供的固定用量聚合函数；运行时不接触 Docker socket，也没有表读取权限。
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
- PostgreSQL `psql` 客户端（Debian/Ubuntu 软件包：`postgresql-client`）
- 主机可以通过回环地址或 Unix socket 访问 Sub2API PostgreSQL
- 数据库所有者需要先运行 `deploy/create_readonly_role.sql`
- 如果使用 webhook，需要 Nginx/Caddy 等反向代理和 HTTPS 域名

### 文件说明

- `sub2api_tg_bot.py`：bot 主程序
- `config.example.json`：绑定配置示例
- `.env.example`：环境变量示例
- `sub2api-tg-bot.service.example`：systemd 服务示例
- `deploy/create_readonly_role.sql`：创建受限登录和固定查询函数

### 创建受限数据库账号

安装机器人前，以 Sub2API 数据库所有者执行：

```bash
docker exec -i sub2api-postgres \
  psql -U sub2api -d sub2api \
  < deploy/create_readonly_role.sql

docker exec -it sub2api-postgres \
  psql -U sub2api -d sub2api \
  -c '\password sub2api_tg_bot'
```

第二条命令会安全地提示输入密码，不会把密码写进命令历史。该账号没有 `api_keys` 或 `usage_logs` 的表读取权限，只能执行 `sub2api_tg_bot_api.usage(text)`。

如果 PostgreSQL 容器尚未向主机开放端口，只绑定到回环地址，例如在 Compose 中配置：

```yaml
ports:
  - "127.0.0.1:5432:5432"
```

不要把 PostgreSQL 的 `5432` 端口暴露到公网。

从旧版升级时，先创建上述数据库函数和账号、安装 `postgresql-client`、确认回环连接可用，再重新运行安装脚本。新版服务不再需要 Docker 权限；不要把 `sub2api-tg-bot` 系统用户加入 `docker` 组。

### 安装

### 安全安装

从完整仓库安装。不要直接执行从 `main` 下载、未经检查的 root 脚本：

```bash
git clone https://github.com/kingboy20230509/sub2api-usage-tg-bot.git
cd sub2api-usage-tg-bot
git log -1 --oneline
less install.sh
sudo bash ./install.sh
```

生产环境建议检出你审核过的发布标签或提交，并记录 `git rev-parse HEAD`。安装脚本只复制当前检出的本地 `sub2api_tg_bot.py`，不会再从另一个仓库或浮动的 `main` 分支下载第二段代码。

脚本会交互式询问：

- Telegram Bot Token
- 要绑定的 Telegram 用户 ID
- 对应的 Sub2API key 名称
- 受限 PostgreSQL 账号 `sub2api_tg_bot` 的密码
- 公开 webhook URL

安装脚本会自动完成：

- 复制当前检出的 `sub2api_tg_bot.py` 到 `/opt/sub2api-tg-bot/`
- 创建无登录 shell 的系统用户 `sub2api-tg-bot`
- 生成 `/opt/sub2api-tg-bot/config.json`
- 生成 `/etc/sub2api-tg-bot.env`
- 生成并启用 `sub2api-tg-bot.service`
- 开启周限额后台巡检（默认每 10 分钟）
- 输出 Nginx 反代示例

也可以从完整仓库目录进行非交互安装：

```bash
sudo env \
  TELEGRAM_BOT_TOKEN="123456:replace_me" \
  TELEGRAM_USER_ID="123456789" \
  SUB2API_KEY_NAME="example-key-name" \
  PGPASSWORD="replace_with_restricted_database_password" \
  PUBLIC_WEBHOOK_URL="https://example.com/tg-sub2api-bot/replace_me" \
  NON_INTERACTIVE=1 \
  bash ./install.sh
```

```bash
sudo mkdir -p /opt/sub2api-tg-bot /etc
sudo groupadd --system sub2api-tg-bot
sudo useradd --system --gid sub2api-tg-bot --home-dir /nonexistent --shell /usr/sbin/nologin sub2api-tg-bot
sudo install -d -o sub2api-tg-bot -g sub2api-tg-bot -m 0700 /var/lib/sub2api-tg-bot
sudo cp sub2api_tg_bot.py /opt/sub2api-tg-bot/
sudo cp config.example.json /opt/sub2api-tg-bot/config.json
sudo cp .env.example /etc/sub2api-tg-bot.env
sudo chown root:sub2api-tg-bot /opt/sub2api-tg-bot/config.json
sudo chmod 600 /etc/sub2api-tg-bot.env
sudo chmod 640 /opt/sub2api-tg-bot/config.json
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
WEBHOOK_SECRET=请替换为至少32位的随机字符串
PUBLIC_WEBHOOK_URL=https://example.com/tg-sub2api-bot/replace_me
WEBHOOK_PATH=/tg-sub2api-bot/replace_me
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8099
ALERT_CHECK_INTERVAL=600
ALERT_STATE_PATH=/var/lib/sub2api-tg-bot/alert_state.json
SUB2API_TG_BOT_CONFIG=/opt/sub2api-tg-bot/config.json
PSQL_BIN=/usr/bin/psql
PGHOST=127.0.0.1
PGPORT=5432
PGDATABASE=sub2api
PGUSER=sub2api_tg_bot
PGPASSWORD=请替换为受限数据库账号密码
PGSSLMODE=prefer
MAX_WEBHOOK_BODY=65536
WEBHOOK_WORKERS=4
WEBHOOK_MAX_PENDING=16
CHECK_COOLDOWN=10
```

`WEBHOOK_SECRET`、`PUBLIC_WEBHOOK_URL` 和 `WEBHOOK_PATH` 都是必填项。公开 URL 必须使用 HTTPS，而且 URL 路径必须与 `WEBHOOK_PATH` 完全一致。建议用以下命令生成 secret：

```bash
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
```

### Nginx webhook 示例

```nginx
# 放在 nginx 的 http 块中，只配置一次
limit_req_zone $binary_remote_addr zone=sub2api_bot:10m rate=2r/s;

location /tg-sub2api-bot/replace_me {
    client_max_body_size 64k;
    limit_req zone=sub2api_bot burst=5 nodelay;
    proxy_pass http://127.0.0.1:8099/tg-sub2api-bot/replace_me;
    proxy_connect_timeout 5s;
    proxy_read_timeout 15s;
    proxy_send_timeout 15s;
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

`/check` 只允许在机器人私聊中使用。每个 Telegram 用户默认每 10 秒只能查询一次。

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
- Shows configured 5-hour, daily, and weekly limits, current usage, and remaining allowance.
- Periodically checks weekly limits and proactively notifies the bound Telegram user when 20% or less remains; one alert per weekly window.
- Requires a Telegram webhook secret token and bounds request size, concurrency, and query frequency.
- Returns usage only in a matching private user chat; group chats never receive usage data.
- Reads bindings from a JSON config file.
- Uses a dedicated PostgreSQL login that can execute only a fixed usage aggregation function; the runtime has no Docker socket or table-read access.
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
- The PostgreSQL `psql` client (`postgresql-client` on Debian/Ubuntu)
- Host access to Sub2API PostgreSQL through loopback or a Unix socket
- A database owner must first run `deploy/create_readonly_role.sql`
- A reverse proxy such as Nginx/Caddy if using Telegram webhook mode

## Files

- `sub2api_tg_bot.py` — bot program
- `config.example.json` — example Telegram user ID to Sub2API key-name bindings
- `.env.example` — example environment variables
- `sub2api-tg-bot.service.example` — example systemd unit
- `deploy/create_readonly_role.sql` — restricted login and fixed query function

## Create the restricted database login

Before installing the bot, run this as the Sub2API database owner:

```bash
docker exec -i sub2api-postgres \
  psql -U sub2api -d sub2api \
  < deploy/create_readonly_role.sql

docker exec -it sub2api-postgres \
  psql -U sub2api -d sub2api \
  -c '\password sub2api_tg_bot'
```

The second command prompts for the password without recording it in shell history. The login cannot read `api_keys` or `usage_logs`; it can only execute `sub2api_tg_bot_api.usage(text)`.

If PostgreSQL is not reachable from the host, publish it on loopback only, for example in Compose:

```yaml
ports:
  - "127.0.0.1:5432:5432"
```

Never expose PostgreSQL port `5432` publicly.

When upgrading from the legacy Docker-backed runtime, create the function and login above, install `postgresql-client`, verify loopback connectivity, and then rerun the installer. The new service needs no Docker access; do not add the `sub2api-tg-bot` system user to the `docker` group.

## Configuration

## Safe installation

Install from a complete checkout. Do not execute an unreviewed root script downloaded from a floating `main` branch:

```bash
git clone https://github.com/kingboy20230509/sub2api-usage-tg-bot.git
cd sub2api-usage-tg-bot
git log -1 --oneline
less install.sh
sudo bash ./install.sh
```

For production, check out a reviewed release tag or commit and record `git rev-parse HEAD`. The installer copies `sub2api_tg_bot.py` from that local checkout and no longer downloads a second-stage program from another repository or a floating branch.

The installer will ask for:

- Telegram Bot Token
- Telegram user ID to bind
- Sub2API key name
- Password for the restricted `sub2api_tg_bot` PostgreSQL login
- Public webhook URL

It will install the bot, generate config/env files, create a systemd service, start it, and print an Nginx reverse proxy example.

For non-interactive installation, run this inside the complete repository checkout:

```bash
sudo env \
  TELEGRAM_BOT_TOKEN="123456:replace_me" \
  TELEGRAM_USER_ID="123456789" \
  SUB2API_KEY_NAME="example-key-name" \
  PGPASSWORD="replace_with_restricted_database_password" \
  PUBLIC_WEBHOOK_URL="https://example.com/tg-sub2api-bot/replace_me" \
  NON_INTERACTIVE=1 \
  bash ./install.sh
```

Copy examples and edit them for your environment:

```bash
sudo mkdir -p /opt/sub2api-tg-bot /etc
sudo groupadd --system sub2api-tg-bot
sudo useradd --system --gid sub2api-tg-bot --home-dir /nonexistent --shell /usr/sbin/nologin sub2api-tg-bot
sudo install -d -o sub2api-tg-bot -g sub2api-tg-bot -m 0700 /var/lib/sub2api-tg-bot
sudo cp sub2api_tg_bot.py /opt/sub2api-tg-bot/
sudo cp config.example.json /opt/sub2api-tg-bot/config.json
sudo cp .env.example /etc/sub2api-tg-bot.env
sudo chown root:sub2api-tg-bot /opt/sub2api-tg-bot/config.json
sudo chmod 600 /etc/sub2api-tg-bot.env
sudo chmod 640 /opt/sub2api-tg-bot/config.json
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
WEBHOOK_SECRET=replace_with_at_least_32_random_characters
PUBLIC_WEBHOOK_URL=https://example.com/tg-sub2api-bot/replace_me
WEBHOOK_PATH=/tg-sub2api-bot/replace_me
LISTEN_HOST=127.0.0.1
LISTEN_PORT=8099
ALERT_CHECK_INTERVAL=600
ALERT_STATE_PATH=/var/lib/sub2api-tg-bot/alert_state.json
SUB2API_TG_BOT_CONFIG=/opt/sub2api-tg-bot/config.json
PSQL_BIN=/usr/bin/psql
PGHOST=127.0.0.1
PGPORT=5432
PGDATABASE=sub2api
PGUSER=sub2api_tg_bot
PGPASSWORD=replace_with_the_restricted_role_password
PGSSLMODE=prefer
MAX_WEBHOOK_BODY=65536
WEBHOOK_WORKERS=4
WEBHOOK_MAX_PENDING=16
CHECK_COOLDOWN=10
```

`WEBHOOK_SECRET`, `PUBLIC_WEBHOOK_URL`, and `WEBHOOK_PATH` are required. The public URL must use HTTPS and its path must exactly match `WEBHOOK_PATH`.

## Nginx webhook example

```nginx
# Configure once in the nginx http block
limit_req_zone $binary_remote_addr zone=sub2api_bot:10m rate=2r/s;

location /tg-sub2api-bot/replace_me {
    client_max_body_size 64k;
    limit_req zone=sub2api_bot burst=5 nodelay;
    proxy_pass http://127.0.0.1:8099/tg-sub2api-bot/replace_me;
    proxy_connect_timeout 5s;
    proxy_read_timeout 15s;
    proxy_send_timeout 15s;
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

Send `/start` or `/check` to the bot in a private chat. Each Telegram user may run `/check` once every 10 seconds by default.

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
