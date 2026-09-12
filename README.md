# Sub2API Usage Telegram Bot

通过 Telegram 私聊查询绑定的 Sub2API API Key 用量。Bot 使用 Telegram 长轮询，不需要域名、Nginx、HTTPS 入口或公开端口。

## 工作方式

```text
Telegram 用户发送 /check
          ↓
Bot 主动通过 HTTPS 长轮询 Telegram getUpdates
          ↓
校验私聊用户并读取 Telegram ID → Key 名称和上游账号 ID 绑定
          ↓
通过内部 Docker 网络调用 PostgreSQL 固定查询函数
          ↓
Bot 通过 Telegram API 返回额度和用量
```

Telegram Bot Token 只用于连接 Telegram。Bot 不保存或使用 Sub2API API Key 的真实密钥；`config.json` 中绑定的是 `api_keys.name` 和非敏感的 `accounts.id`。
管理员确认批量重置时，Bot 先通过固定数据库函数保存速率限制快照，再使用独立的 Sub2API Admin API Key 逐个调用 Sub2API 内网管理接口，随后复查每个 Key 的结果。

## 功能与安全边界

- `/start` 显示使用提示，`/check` 查询用量；管理员可以查看所有已绑定 Key 的最后使用时间、每周额度进度条、今日/昨日去重 IP 数量，并通过主页底部独立的“IP 使用记录”入口查看具体 Key 近 3 个自然日的去重 IP、首次/最近时间和请求次数；普通用户的查询内容不变。
- 管理员还可以查看绑定账号的周使用百分比、周重置时间、消耗金额和满额金额预估，也可以通过复选按钮选择一个或多个 Key，在最终确认后批量重置 5 小时/日/周限速用量。
- 显示 Key 到期时间、总额度、5 小时/日/周限额、Key 自身的 5 小时与每周重置时间、今日及 7 天用量和模型统计；上游账号重置时间只在管理员 Key 总览中明确标注，7 天用量按 Asia/Shanghai 时区的 7 个自然日统计。
- 周额度剩余不超过 20% 时主动提醒，每个周窗口只提醒一次。
- 上游账号自然到达原计划时间后更新 7 日重置时间时，Bot 不重置 Key 且不通知；只有原计划时间尚未到却提前跳到新周期时，才进入该账号绑定 Key 的随号重置审批。多个账号同时触发时会逐个排队处理。
- 手动重置前会完整备份三个用量及三个窗口开始时间，每个 Key 保留最近 3 份；管理员可在 `/check` 菜单中选择单 Key 备份回滚，也可选择完整覆盖当前全部绑定 Key 的批次版本执行全员回滚。
- 重置成功后会向管理员发送重置前快照，格式与 Key 总览一致，包含最后使用时间、每周已用/限额/剩余及进度条。
- 仅允许绑定用户在与 Bot 的私聊中查询；群聊不返回用量。
- 限制并发、待处理消息数和每用户查询频率。
- 使用专用 PostgreSQL 登录且无表级权限，只能执行固定函数；账号查询不返回凭据，受控写函数仅用于保存和恢复速率限制快照。
- 普通用户看不到重置按钮；所有重置回调都会在服务端重新校验 Telegram 管理员 ID，不能通过伪造回调绕过。
- 重置通过 Sub2API 官方管理接口完成，以同步失效认证与限速缓存；回滚先用同一接口定向失效缓存，再由固定数据库函数恢复六个速率限制字段。
- 不访问 Docker Socket，不需要 root。
- Docker 根文件系统只读、删除全部 capabilities，并使用 UID/GID `10001`。
- Telegram Token、数据库密码和 Sub2API Admin API Key 支持 Compose file-backed secrets。

## 文件

- `sub2api_tg_bot.py`：Bot 主程序。
- `Dockerfile`：非 root 容器镜像。
- `compose.example.yaml`：合并进现有 Sub2API Compose 的示例。
- `docker-healthcheck.py`：仅在容器内部访问的存活检查。
- `config.example.json`：Telegram ID 到 Key 名称和上游账号 ID 的绑定示例。
- `.env.docker.example`：非敏感 Docker 参数。
- `deploy/create_readonly_role.sql`：受限数据库账号与固定查询函数。
- `install.sh`、`sub2api-tg-bot.service.example`：可选 systemd 部署。

## 1. 初始化 Sub2API 数据库

必须先启动 Sub2API，让它完成数据库迁移。确认以下三张表存在：

```bash
docker compose exec -T postgres psql -U sub2api -d sub2api -tAc \
  "SELECT to_regclass('public.api_keys'), to_regclass('public.usage_logs'), to_regclass('public.accounts');"
```

正常结果为：

```text
api_keys|usage_logs|accounts
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

将 BotFather 提供的 Token 写入 `secrets/telegram_bot_token`，将 `sub2api_tg_bot` 数据库密码写入 `secrets/postgres_password`，并将 Sub2API 系统设置中的 Admin API Key 写入 `secrets/sub2api_admin_api_key`。Admin API Key 权限较高，必须使用独立 secret 文件，不能写进 `config.json` 或提交到 Git：

```bash
chmod 600 secrets/telegram_bot_token secrets/postgres_password secrets/sub2api_admin_api_key
```

编辑 `config.json`：

```json
{
  "admins": [
    "123456789"
  ],
  "bindings": {
    "123456789": {
      "key_name": "Administrator",
      "account_id": 12
    },
    "987654321": {
      "key_name": "example-key-name",
      "account_id": 15
    }
  },
  "timezone": "Asia/Shanghai"
}
```

`admins` 中填写管理员的 Telegram 数字用户 ID。`bindings` 左边是 Telegram 数字用户 ID 字符串；`key_name` 是 Sub2API 数据库中准确的 `api_keys.name`；`account_id` 是该 Key 要参考的上游账号 `accounts.id`。同一个 Key 可以绑定给多个 Telegram 用户，但这些重复绑定的 `account_id` 必须完全一致；Bot 启动时会拒绝存在跨账号同名 Key 的配置，避免自动重置错位。管理员发送 `/check` 后会看到所有 `bindings` 的 Key 按钮，普通用户只能查询自己的绑定。按钮文字直接使用 Key 名称，不需要额外的 `label`。Key 总览中的最后使用时间取自该 Key 最新一条 `usage_logs.created_at`；IP 数据取自 `usage_logs.ip_address`，今日和昨日按 `Asia/Shanghai` 自然日去重；近 3 日详情为今天及前两个自然日，只允许管理员查看。

在 Sub2API Compose 目录执行下面的只读查询，找到账号 ID：

```bash
docker compose exec -T postgres psql -U sub2api -d sub2api -P pager=off \
  -c "SELECT id, name, platform, type, status, extra->>'codex_usage_updated_at' AS snapshot_updated_at, extra->>'codex_7d_reset_at' AS reset_7d_at FROM accounts WHERE deleted_at IS NULL ORDER BY id;"
```

`account_id` 必须按账号逐个绑定，不能用 Key 名称推断。Bot 使用 `accounts.extra` 中由 Sub2API 保存的 `codex_5h_reset_at`、`codex_7d_reset_at` 和 `codex_7d_used_percent`，仅在管理员的 Key 总览“上游账号信息”区域展示；单个 Key 限额区域的重置时间来自 `api_keys` 自身窗口。管理员的 Key 总览还会按账号当前周窗口汇总 Sub2API 账号成本，并用“消耗金额 ÷ 使用百分比”线性预估满额金额。该快照可能在账号尚未使用或后台尚未刷新时为空或过期；百分比为 0 或数据不完整时不会进行预估。Bot 不会伪造重置周期，也不会使用管理员 Token 强制刷新。旧版字符串绑定仍可继续使用，但不会显示上游账号重置时间或账号金额预估。

Key 名称必须唯一。如果数据库中存在多个未删除且同名的 Key，Bot 会拒绝返回数据并提示先改成唯一名称，避免误显示其他 Key 的用量。生产 Linux 主机使用：

```bash
chown 10001:10001 config.json
chmod 600 config.json
```

## 3. 合并 Compose 服务

将 `compose.example.yaml` 中的 Bot 服务、三个 secret、状态卷和网络合并到现有 Sub2API Compose。`SUB2API_TG_BOT_SUB2API_BASE_URL` 必须使用 Sub2API 的 Compose 服务名和容器内部端口，默认是 `http://sub2api:8080`；不要填写浏览器访问的公网地址。

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

普通用户直接查询自己的 Key；管理员会先看到 Key 查询按钮、“Key 总览”、“批量重置速率限制”、“回滚 Key 使用量”和主页最下方的“IP 使用记录”按钮。普通用户和管理员查看单个 Key 时都不会显示上游账号信息。“Key 总览”按 Key 名称去重，每页显示 8 个 Key，展示最后使用时间、每周已用/限额/剩余、Key 自身的周重置时间和 12 格进度条；下方“上游账号信息”按 `account_id` 去重展示账号周使用百分比、明确标注的上游周重置时间、账号消耗金额、满额金额预估及汇总，最后显示本次刷新时间。账号名称若为邮箱格式会自动脱敏。总览下方的 Key 按钮用于打开对应 Key 的完整用量内容；今日/昨日去重 IP 数量继续保留在总览正文。具体 IP 地址统一从主页“IP 使用记录”进入，选择 Key 后查看近 3 天记录，详情返回时回到 IP Key 列表。总览支持刷新、翻页和返回，不显示请求、Tokens、模型、5 小时或每日信息。

账号金额预估只对配置中的管理员开放。普通用户看不到 Key 总览按钮；即使伪造 `overview` 回调，Bot 也会在执行任何 Key 或账号数据库查询前重新校验 Telegram 管理员 ID。账号消耗金额采用 Sub2API 的账号用量统计口径，不代表 OpenAI 实际账单；满额金额是线性预估值。

进入批量重置后，可以逐项勾选或取消 Key，也可以全选或清空；点击“重置所选”后还需最终确认。Bot 会先为每个 Key 备份 `usage_5h`、`usage_1d`、`usage_7d`、`window_5h_start`、`window_1d_start`、`window_7d_start`，并保存重置前的 `last_used_at` 与 `rate_limit_7d` 用于通知展示；备份成功才调用管理接口重置。所有选中 Key 的 5 小时、每日和 7 日窗口使用同一个批次重置时间，因此 7 日剩余时间会一起从 7 天开始倒计时。单个 Key 失败不会中断其余 Key，完成消息会分别汇总成功、需复查和失败数量，重置成功项会另行向管理员发送与 Key 总览相同格式的重置前数据。

每个 Key 只保留最近 3 份重置前备份。在“回滚 Key 使用量”中可选择单 Key 回滚，或选择一个完整覆盖当前全部绑定 Key 的批次版本执行全员回滚；两种方式都要求再次确认。Bot 会先通过官方管理接口定向清除目标 Key 的数据库计数与 Redis 限速缓存，再立即从备份恢复上述六个字段并复查。回滚不修改 `quota_used`，也不删除历史用量记录。升级前产生的旧备份没有批次 ID，只能继续用于单 Key 回滚，不会按相近时间自动拼接为全员版本。

重置不会清零累计总额度 `quota_used`，也不会删除今日或近 7 天历史用量记录。普通用户不会看到重置按钮，即使伪造 Telegram 回调也会被管理员 ID 校验拒绝。

批量选择会话只保存在 Bot 内存中，按管理员及当前 Telegram 消息隔离，并在 5 分钟后过期。Bot 重启、配置中的 Key 被移除或重复点击已执行的确认按钮，都不会再次执行旧批次。批量重置和回滚只操作现有 `bindings`，不需要修改 `config.json` 结构。

手动重置使用 Sub2API 官方管理员接口清零用量并定向失效缓存，再通过受控数据库函数统一设置本批次的窗口起点。只有备份成功后才会执行重置。

Bot 每 60 秒分别读取各个已绑定上游账号保存的 7 日重置时间。第一次读取只建立基线；新时间在原计划时间到达后出现属于自然重置，Bot 只更新该账号基线，不重置任何 Key，也不发送消息。如果原计划时间尚未到，新时间却向后推进至少 1 小时，则判定为 OpenAI 突然重置，并向唯一管理员发送以“通知：”开头的询问。管理员可以选择“对齐并重置此账号 Key”或“本次不重置”；3 分钟未操作会自动执行。多个账号先后或同时触发时，事件会保存在状态文件中并逐个询问，不会用一个账号的时间处理另一个账号的 Key。

批准或超时后，Bot 只为触发账号当前绑定的唯一 Key 创建备份，再清零 5 小时、每日和 7 日用量，并把这些 Key 的三个窗口统一对齐到“该账号新的上游 7 日重置时间减 7 天”。只有确认成功的 Key 对应用户会收到以“公告：”开头的“OpenAI重置，随号重置”；失败或需复查的 Key 用户不收到公告，其他账号的用户也不会收到本次公告。拒绝后不重置、不公告，同一事件不会重复询问。

Key 总览只使用现有 `bindings` 和只读数据库函数，不依赖 Sub2API Admin API Key。突然重置监控依赖现有 `account_id`、Sub2API Admin API Key，以及持久化的 `AUTO_RESET_STATE_PATH`。本功能升级不需要修改 secret 或 `config.json`，但必须合并新的 Compose 环境变量，并以数据库所有者重新执行 `deploy/create_readonly_role.sql`，以安装轻量级账号时间查询、备份、对齐和恢复函数及其最小执行权限。

普通用户的数据库查询冷却默认是 10 秒，管理员切换或刷新 Key 的冷却默认是 2 秒。可分别通过 `.env` 中的 `SUB2API_TG_BOT_CHECK_COOLDOWN` 和 `SUB2API_TG_BOT_ADMIN_CHECK_COOLDOWN` 调整。

## 容器更新

```bash
git pull --ff-only
docker compose exec -T postgres psql -U sub2api -d sub2api \
  < deploy/create_readonly_role.sql
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
- 同名 Key：Sub2API 中有多个未删除的 Key 使用了相同名称，请先修改为唯一名称。
- 找不到上游账号：`account_id` 不存在、账号已删除，或数据库函数尚未重新部署。
- 配置提示同一个 Key 绑定到不同账号：检查所有重复 `key_name`，确保它们的 `account_id` 完全一致；不需要修改 `config.json` 的字段或结构。
- Key 没有重置时间：该 Key 尚未建立对应限额窗口；产生一次用量或通过 Bot 手动重置后再查询。
- 上游没有重置时间：对应账号还没有 Codex 用量快照；先在 Sub2API 中确认该账号的用量数据已刷新。
- 回滚列表为空：只有升级后实际执行过且备份成功的手动重置才会产生新备份；已经存在的旧自动重置备份仍可回滚，升级前的误重置不会自动补出旧数据。
- 没有重置按钮：检查 `SUB2API_BASE_URL` 和 `SUB2API_ADMIN_API_KEY_FILE` 是否同时配置，并确认 secret 文件存在。
- 重置返回 `401`：`secrets/sub2api_admin_api_key` 与 Sub2API 系统设置中的 Admin API Key 不一致。
- 无法连接重置接口：确认 Bot 与 Sub2API 服务位于同一个内部 Compose 网络，且 `SUB2API_TG_BOT_SUB2API_BASE_URL` 使用服务名和容器内部端口。
- 批量选择已过期：重新发送 `/check`，再次进入批量重置；未确认的选择不会执行。
- 突然重置未触发：首次启动只会建立上游时间基线；确认 `AUTO_RESET_STATE_PATH` 位于持久化卷，并检查绑定中的 `account_id` 是否正确。

容器健康检查访问 `127.0.0.1:8099/health`，该端口只在容器内部监听且不发布到宿主机。

## systemd 部署（可选）

systemd 部署同样使用长轮询，不需要域名或 Nginx。先安装 Python 3.10+ 和 `postgresql-client`，然后从完整、已审核的仓库目录运行：

```bash
sudo bash ./install.sh
```

安装脚本会询问 Telegram Token、用户 ID、Key 名称和受限数据库密码，并创建独立系统用户与加固后的服务。
如需启用管理员重置，再在 `/etc/sub2api-tg-bot.env` 中配置 `SUB2API_BASE_URL` 和 `SUB2API_ADMIN_API_KEY_FILE`，并确保服务用户可以读取对应的 `0600` secret 文件。

---

## English

This bot checks a Telegram user's bound Sub2API key usage through Telegram long polling. It requires no public domain, reverse proxy, TLS endpoint, inbound port, or Docker socket.

### Architecture

The bot calls Telegram `getUpdates` over outbound HTTPS, authorizes the private Telegram user, maps the user ID to an `api_keys.name`, calls the fixed PostgreSQL function over an isolated Compose network, and returns the result with `sendMessage`.

### Docker deployment

1. Start Sub2API and let database migrations complete.
2. Run `deploy/create_readonly_role.sql` as the database owner and set a unique password for `sub2api_tg_bot`.
3. Copy `config.example.json` to `config.json` and bind Telegram IDs to exact key names and upstream account IDs.
4. Create file-backed secrets for the Telegram Token, restricted database password, and Sub2API Admin API Key.
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

Bot admins can reset the selected key's 5-hour, daily, and 7-day rate-limit counters after a second confirmation. Every reset first stores all six rate-limit values, keeping the latest three backups per key. Admins can select one backup to restore through the bot. Regular Telegram users cannot invoke reset or rollback callbacks.

The bot treats a 7-day timestamp advance as natural when the previous scheduled reset time has already arrived; natural resets only update the saved baseline and remain silent. If the timestamp advances by at least one hour before the previous reset was due, the bot asks the configured admin whether all unique bound keys should be backed up, reset, and aligned to the new upstream cycle start. Approval runs immediately, rejection skips the event, and no response runs it after three minutes. Only users whose keys are confirmed successful receive the public announcement. The bot polls Sub2API's saved snapshot rather than contacting the upstream provider directly.

## License

MIT
