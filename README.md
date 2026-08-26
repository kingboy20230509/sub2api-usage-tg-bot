/opt/homebrew/Library/Homebrew/cmd/shellenv.sh: line 27: /bin/ps: Operation not permitted
# Sub2API Usage Telegram Bot

通过 Telegram 私聊查询绑定的 Sub2API API Key 用量。Bot 使用 Telegram 长轮询，不需要域名、Nginx、HTTPS 入口或公开端口。

## 工作方式

```text
Telegram 用户发送 /check
          ↓
Bot 主动通过 HTTPS 长轮询 Telegram getUpdates
          ↓
校验私聊用户并读取 Telegram ID → Key 名称绑定
          ↓
通过内部 Docker 网络调用 PostgreSQL 固定查询函数
          ↓
Bot 通过 Telegram API 返回额度和用量
```

Telegram Bot Token 只用于连接 Telegram。Bot 不保存或使用 Sub2API API Key 的真实密钥；`config.json` 中绑定的是 `api_keys.name`。

## 功能与安全边界

- `/start` 显示使用提示，`/check` 查询用量。
- 显示总额度、5 小时/日/周限额、今日及 7 天用量和模型统计；周限额显示账号实际重置周期，7 天用量按 Asia/Shanghai 时区的 7 个自然日统计。
- 周额度剩余不超过 20% 时主动提醒，每个周窗口只提醒一次。
- 仅允许绑定用户在与 Bot 的私聊中查询；群聊不返回用量。
- 限制并发、待处理消息数和每用户查询频率。
- 使用专用 PostgreSQL 登录，只能执行固定的 `sub2api_tg_bot_api.usage(text)` 函数。
- 不访问 Docker Socket，不需要 root，不直接读取或修改 Sub2API 数据表。
- Docker 根文件系统只读、删除全部 capabilities，并使用 UID/GID `10001`。
- Token 和数据库密码支持 Compose file-backed secrets。

## 文件

- `sub2api_tg_bot.py`：Bot 主程序。
- `Dockerfile`：非 root 容器镜像。
- `compose.example.yaml`：合并进现有 Sub2API Compose 的示例。
- `docker-healthcheck.py`：仅在容器内部访问的存活检查。
- `config.example.json`：Telegram ID 到 Key 名称的绑定示例。
- `.env.docker.example`：非敏感 Docker 参数。
- `deploy/create_readonly_role.sql`：受限数据库账号与固定查询函数。
- `install.sh`、`sub2api-tg-bot.service.example`：可选 systemd 部署。

## 1. 初始化 Sub2API 数据库

必须先启动 Sub2API，让它完成数据库迁移。确认以下两张表存在：

```bash
docker compose exec -T postgres psql -U sub2api -d sub2api -tAc \
  "SELECT to_regclass('public.api_keys'), to_regclass('public.usage_logs');"
```

正常结果为：

```text
api_keys|usage_logs
```

然后以数据库所有者执行：

```bash
docker compose exec -T postgres psql -U sub2api -d sub2api \
  < deploy/create_readonly_role.sql

docker compose exec postgres psql -U sub2api -d sub2api \
  -c '\password sub2api_tg_bot'
```

第二条命令安全提示输入独立密码。不要复用 PostgreSQL 管理员密码。

## 2. 准备 Docker 配置

```bash
cp config.example.json config.json
mkdir -m 700 secrets
```

将 BotFather 提供的 Token 写入 `secrets/telegram_bot_token`，将 `sub2api_tg_bot` 数据库密码写入 `secrets/postgres_password`：

```bash
chmod 600 secrets/telegram_bot_token secrets/postgres_password
```

编辑 `config.json`：

```json
{
  "bindings": {
    "123456789": "example-key-name"
  },
  "timezone": "Asia/Shanghai"
}
```

左边是 Telegram 数字用户 ID 字符串；右边是 Sub2API 数据库中准确的 `api_keys.name`。生产 Linux 主机使用：

```bash
chown 10001:10001 config.json
chmod 600 config.json
```

## 3. 合并 Compose 服务

将 `compose.example.yaml` 中的 Bot 服务、两个 secret、状态卷和网络合并到现有 Sub2API Compose。

关键网络关系：

```yaml
services:
  sub2api:
    networks: [sub2api-app, sub2api-db]

  postgres:
    networks: [sub2api-db]

  sub2api-tg-bot:
    networks: [sub2api-bot-egress, sub2api-db]

networks:
  sub2api-app:
  sub2api-bot-egress:
  sub2api-db:
    internal: true
```

Bot 通过非内部的 `sub2api-bot-egress` 主动访问 Telegram，通过 `internal: true` 的 `sub2api-db` 访问 PostgreSQL。不要为 Bot 或 PostgreSQL配置 `ports`，不要挂载 `/var/run/docker.sock`。

Compose 中 `PGHOST` 必须等于 PostgreSQL 的服务名，例如 `postgres`。标准 PostgreSQL 容器的内部网络通常不启用 TLS，因此示例显式设置：

```yaml
PGSSLMODE: disable
PG_ALLOW_INSECURE_PRIVATE_NETWORK: "1"
```

该例外只接受单段 Compose 服务名，应当始终配合隔离的数据库网络。

## 4. 构建和启动

```bash
docker compose config --quiet
docker compose build --pull sub2api-tg-bot
docker compose up -d sub2api-tg-bot
docker compose ps sub2api-tg-bot
docker compose logs --tail=100 sub2api-tg-bot
```

正常启动日志包含：

```text
sub2api tg bot long polling started
```

程序启动时会调用 `deleteWebhook`，然后使用 `getUpdates`。同一个 Token 只能运行一个长轮询实例。

## 5. 使用

使用已绑定的 Telegram 账号私聊 Bot：

```text
/start
/check
```

`/check` 默认每个用户 10 秒只能执行一次。

## 容器更新

```bash
git pull --ff-only
docker compose build --pull sub2api-tg-bot
docker compose up -d sub2api-tg-bot
```

## 排查

查看日志：

```bash
docker compose logs --tail=200 sub2api-tg-bot
```

常见问题：

- `Conflict: terminated by other getUpdates request`：同一个 Token 运行了另一个 Bot 实例。
- 数据库认证失败：数据库角色密码与 Compose secret 内容不一致。
- `permission denied`：`config.json` 没有设置为 UID/GID `10001` 可读。
- 未绑定：`config.json` 的 Telegram ID 不匹配。
- 查不到 Key：绑定值不是准确的 `api_keys.name`。

容器健康检查访问 `127.0.0.1:8099/health`，该端口只在容器内部监听且不发布到宿主机。

## systemd 部署（可选）

systemd 部署同样使用长轮询，不需要域名或 Nginx。先安装 Python 3.10+ 和 `postgresql-client`，然后从完整、已审核的仓库目录运行：

```bash
sudo bash ./install.sh
```

安装脚本会询问 Telegram Token、用户 ID、Key 名称和受限数据库密码，并创建独立系统用户与加固后的服务。

---

## English

This bot checks a Telegram user's bound Sub2API key usage through Telegram long polling. It requires no public domain, reverse proxy, TLS endpoint, inbound port, or Docker socket.

### Architecture

The bot calls Telegram `getUpdates` over outbound HTTPS, authorizes the private Telegram user, maps the user ID to an `api_keys.name`, calls the fixed PostgreSQL function over an isolated Compose network, and returns the result with `sendMessage`.

### Docker deployment

1. Start Sub2API and let database migrations complete.
2. Run `deploy/create_readonly_role.sql` as the database owner and set a unique password for `sub2api_tg_bot`.
3. Copy `config.example.json` to `config.json` and bind Telegram IDs to exact key names.
4. Create file-backed secrets for the Telegram Token and restricted database password.
5. Merge `compose.example.yaml` into the existing Compose project.
6. Attach PostgreSQL, Sub2API, and the bot to an `internal: true` database network. Give the bot a separate non-internal egress network for Telegram.
7. Publish no bot or PostgreSQL ports.

Build and start:

```bash
docker compose config --quiet
docker compose build --pull sub2api-tg-bot
docker compose up -d sub2api-tg-bot
docker compose logs --tail=100 sub2api-tg-bot
```

The expected startup message is `sub2api tg bot long polling started`. Only one polling process may use a Telegram Bot Token at a time.

## License

MIT
