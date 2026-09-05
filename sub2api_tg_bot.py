#!/usr/bin/env python3
import json
import os
import re
import secrets
import signal
import threading
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo


def read_secret(name):
    value = os.environ.get(name, "")
    file_path = os.environ.get(f"{name}_FILE", "").strip()
    if value and file_path:
        raise RuntimeError(f"Set only one of {name} or {name}_FILE")
    if not file_path:
        return value
    if not os.path.isabs(file_path):
        raise RuntimeError(f"{name}_FILE must be an absolute path")
    try:
        with open(file_path, "r", encoding="utf-8") as secret_file:
            value = secret_file.read(4097)
    except OSError as error:
        raise RuntimeError(f"Unable to read {name}_FILE") from error
    if len(value) > 4096:
        raise RuntimeError(f"{name}_FILE is too large")
    return value.rstrip("\r\n")


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("SUB2API_TG_BOT_CONFIG", os.path.join(BASE_DIR, "config.json"))
TOKEN = read_secret("TELEGRAM_BOT_TOKEN").strip()
SUB2API_BASE_URL = os.environ.get("SUB2API_BASE_URL", "").strip().rstrip("/")
SUB2API_ADMIN_API_KEY = read_secret("SUB2API_ADMIN_API_KEY").strip()
SUB2API_ADMIN_TIMEOUT = int(os.environ.get("SUB2API_ADMIN_TIMEOUT", "10"))
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8099"))
ALERT_STATE_PATH = os.environ.get("ALERT_STATE_PATH", os.path.join(BASE_DIR, "alert_state.json"))
ALERT_CHECK_INTERVAL = int(os.environ.get("ALERT_CHECK_INTERVAL", "600"))
PSQL_BIN = os.environ.get("PSQL_BIN", "/usr/bin/psql").strip()
PGHOST = os.environ.get("PGHOST", "127.0.0.1").strip()
PGPORT = os.environ.get("PGPORT", "5432").strip()
PGDATABASE = os.environ.get("PGDATABASE", "sub2api").strip()
PGUSER = os.environ.get("PGUSER", "sub2api_tg_bot").strip()
PGPASSWORD = read_secret("PGPASSWORD")
PGSSLMODE = os.environ.get("PGSSLMODE", "prefer").strip()
PG_ALLOW_INSECURE_PRIVATE_NETWORK = os.environ.get("PG_ALLOW_INSECURE_PRIVATE_NETWORK", "0").strip()
UPDATE_WORKERS = int(os.environ.get("UPDATE_WORKERS", "4"))
UPDATE_MAX_PENDING = int(os.environ.get("UPDATE_MAX_PENDING", "16"))
CHECK_COOLDOWN = int(os.environ.get("CHECK_COOLDOWN", "10"))
ADMIN_CHECK_COOLDOWN = int(os.environ.get("ADMIN_CHECK_COOLDOWN", "2"))
POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "10"))
API = f"https://api.telegram.org/bot{TOKEN}"
KEY_NAME_RE = re.compile(r"^[\w .:@+-]{1,100}$")
PG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,62}$")
COMPOSE_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
PSQL_VARIABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RATE_LIMIT_LOCK = threading.Lock()
_LAST_CHECK_BY_USER = {}
_BATCH_RESET_LOCK = threading.Lock()
_BATCH_RESET_SESSIONS = {}
_RESET_OPERATION_LOCK = threading.Lock()
BATCH_RESET_SESSION_TTL = 300
OVERVIEW_PAGE_SIZE = 8
IP_HISTORY_PAGE_SIZE = 10


def log_failure(event, error):
    print(f"{event} failed error={type(error).__name__}", file=sys.stderr, flush=True)


def masked_id(value):
    value = str(value or "")
    return "***" + value[-4:] if value else "unknown"


def validate_runtime_config():
    if not TOKEN or ":" not in TOKEN or "replace_me" in TOKEN.lower():
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing or invalid")
    if not 1 <= UPDATE_WORKERS <= 32:
        raise RuntimeError("UPDATE_WORKERS must be between 1 and 32")
    if not UPDATE_WORKERS <= UPDATE_MAX_PENDING <= 256:
        raise RuntimeError("UPDATE_MAX_PENDING must be between UPDATE_WORKERS and 256")
    if not 1 <= CHECK_COOLDOWN <= 3600:
        raise RuntimeError("CHECK_COOLDOWN must be between 1 and 3600 seconds")
    if not 1 <= ADMIN_CHECK_COOLDOWN <= 3600:
        raise RuntimeError("ADMIN_CHECK_COOLDOWN must be between 1 and 3600 seconds")
    if not 1 <= POLL_TIMEOUT <= 50:
        raise RuntimeError("POLL_TIMEOUT must be between 1 and 50 seconds")
    if not 1 <= SUB2API_ADMIN_TIMEOUT <= 60:
        raise RuntimeError("SUB2API_ADMIN_TIMEOUT must be between 1 and 60 seconds")
    if bool(SUB2API_BASE_URL) != bool(SUB2API_ADMIN_API_KEY):
        raise RuntimeError("SUB2API_BASE_URL and SUB2API_ADMIN_API_KEY must be configured together")
    if SUB2API_BASE_URL:
        parsed = urllib.parse.urlsplit(SUB2API_BASE_URL)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("SUB2API_BASE_URL is invalid")
        try:
            parsed.port
        except ValueError as error:
            raise RuntimeError("SUB2API_BASE_URL is invalid") from error
        local_hosts = {"127.0.0.1", "::1", "localhost"}
        if (
            parsed.scheme == "http"
            and parsed.hostname not in local_hosts
            and not COMPOSE_SERVICE_NAME_RE.fullmatch(parsed.hostname)
        ):
            raise RuntimeError("SUB2API_BASE_URL must use HTTPS outside a private Compose network")
        if (
            not SUB2API_ADMIN_API_KEY
            or "replace_me" in SUB2API_ADMIN_API_KEY.lower()
            or any(char in SUB2API_ADMIN_API_KEY for char in "\r\n\0")
        ):
            raise RuntimeError("SUB2API_ADMIN_API_KEY is invalid")
    if not os.path.isabs(PSQL_BIN) or not os.path.isfile(PSQL_BIN) or not os.access(PSQL_BIN, os.X_OK):
        raise RuntimeError("PSQL_BIN must point to an executable psql client")
    if not PGHOST or any(char in PGHOST for char in "\r\n\0"):
        raise RuntimeError("PGHOST is missing or invalid")
    if not PGPORT.isdigit() or not 1 <= int(PGPORT) <= 65535:
        raise RuntimeError("PGPORT must be between 1 and 65535")
    if not PG_NAME_RE.fullmatch(PGDATABASE) or not PG_NAME_RE.fullmatch(PGUSER):
        raise RuntimeError("PGDATABASE and PGUSER contain invalid characters")
    if not PGPASSWORD or "replace_me" in PGPASSWORD.lower() or any(char in PGPASSWORD for char in "\r\n\0"):
        raise RuntimeError("PGPASSWORD must contain the read-only bot database password")
    if PGSSLMODE not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise RuntimeError("PGSSLMODE is invalid")
    if PG_ALLOW_INSECURE_PRIVATE_NETWORK not in {"0", "1"}:
        raise RuntimeError("PG_ALLOW_INSECURE_PRIVATE_NETWORK must be 0 or 1")
    database_is_local = PGHOST in {"127.0.0.1", "::1", "localhost"} or PGHOST.startswith("/")
    database_uses_tls = PGSSLMODE in {"require", "verify-ca", "verify-full"}
    if not database_is_local and not database_uses_tls:
        if (
            PG_ALLOW_INSECURE_PRIVATE_NETWORK != "1"
            or PGSSLMODE != "disable"
            or not COMPOSE_SERVICE_NAME_RE.fullmatch(PGHOST)
        ):
            raise RuntimeError(
                "Remote PostgreSQL must use TLS, or explicitly allow a single-label Compose service "
                "on a trusted private network with PGSSLMODE=disable"
            )


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def config_bindings(config):
    bindings = config.get("bindings") or {}
    if not isinstance(bindings, dict):
        raise ValueError("bindings must be an object")
    normalized = {}
    for user_id, value in bindings.items():
        user_id = str(user_id)
        if isinstance(value, str):
            key_name = value
            account_id = None
        elif isinstance(value, dict):
            if set(value) != {"key_name", "account_id"}:
                raise ValueError("Binding objects must contain only key_name and account_id")
            key_name = value.get("key_name")
            account_id = value.get("account_id")
            if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
                raise ValueError("account_id must be a positive integer")
        else:
            raise ValueError("Binding must be a key name string or an object")
        if not user_id.isdigit() or not isinstance(key_name, str) or not KEY_NAME_RE.fullmatch(key_name):
            raise ValueError("Invalid Telegram ID or key name in binding config")
        normalized[user_id] = {"key_name": key_name, "account_id": account_id}
    return normalized


def config_admins(config):
    admins = config.get("admins") or []
    if not isinstance(admins, list):
        raise ValueError("admins must be an array")
    normalized = {str(user_id) for user_id in admins}
    if any(not user_id.isdigit() for user_id in normalized):
        raise ValueError("Invalid Telegram ID in admins config")
    return normalized


def reset_candidates(bindings):
    candidates = []
    seen_key_names = set()
    sorted_bindings = sorted(bindings.items(), key=lambda item: (item[1]["key_name"].casefold(), item[0]))
    for target_user_id, binding in sorted_bindings:
        key_name = binding["key_name"]
        if key_name in seen_key_names:
            continue
        seen_key_names.add(key_name)
        candidates.append((target_user_id, binding))
    return candidates


def batch_reset_keyboard(bindings, selected_user_ids):
    selected_user_ids = set(selected_user_ids)
    candidate_ids = {target_user_id for target_user_id, _binding in reset_candidates(bindings)}
    selected_user_ids.intersection_update(candidate_ids)
    buttons = []
    for target_user_id, binding in reset_candidates(bindings):
        prefix = "✅ " if target_user_id in selected_user_ids else "⬜ "
        key_name = binding["key_name"]
        max_name_length = 64 - len(prefix)
        button_text = prefix + (
            key_name if len(key_name) <= max_name_length else key_name[:max_name_length - 3] + "..."
        )
        buttons.append({"text": button_text, "callback_data": f"batch_toggle:{target_user_id}"})
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.extend([
        [
            {"text": "☑️ 全选", "callback_data": "batch_all:0"},
            {"text": "清空", "callback_data": "batch_clear:0"},
        ],
        [{
            "text": f"🔴 重置所选（{len(selected_user_ids)}）",
            "callback_data": "batch_review:0",
        }],
        [{"text": "取消", "callback_data": "batch_cancel:0"}],
    ])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def batch_reset_selection_text(selected_count, total_count):
    return (
        "🔄 批量重置速率限制\n\n"
        "请选择需要重置的 Key。再次点击可取消选择。\n"
        f"已选择：{selected_count} / {total_count}"
    )


def batch_reset_review_text(bindings, selected_user_ids):
    selected_user_ids = set(selected_user_ids)
    selected_candidates = [
        binding for target_user_id, binding in reset_candidates(bindings)
        if target_user_id in selected_user_ids
    ]
    lines = [
        "⚠️ 确认批量重置？",
        "",
        f"即将重置以下 {len(selected_candidates)} 个 Key：",
        "",
    ]
    for binding in selected_candidates[:30]:
        lines.append(f"• {binding['key_name']}")
    if len(selected_candidates) > 30:
        lines.append(f"• …另有 {len(selected_candidates) - 30} 个 Key")
    lines.extend([
        "",
        "将清零 5 小时、每日和 7 天限速计数。",
        "不会清零总额度 quota_used，也不会删除历史用量记录。",
    ])
    return "\n".join(lines)


def batch_reset_confirmation_keyboard(selected_count):
    return json.dumps({"inline_keyboard": [
        [{"text": f"✅ 确认重置 {selected_count} 个 Key", "callback_data": "batch_confirm:0"}],
        [
            {"text": "◀️ 返回选择", "callback_data": "batch_back:0"},
            {"text": "取消", "callback_data": "batch_cancel:0"},
        ],
    ]}, ensure_ascii=False)


def start_batch_reset_session(admin_user_id, chat_id, message_id, now=None):
    now = time.monotonic() if now is None else now
    with _BATCH_RESET_LOCK:
        _BATCH_RESET_SESSIONS[str(admin_user_id)] = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "selected": set(),
            "updated_at": now,
        }
    return set()


def change_batch_reset_selection(
    admin_user_id,
    chat_id,
    message_id,
    valid_user_ids,
    operation,
    target_user_id=None,
    now=None,
):
    now = time.monotonic() if now is None else now
    valid_user_ids = set(valid_user_ids)
    with _BATCH_RESET_LOCK:
        admin_user_id = str(admin_user_id)
        session = _BATCH_RESET_SESSIONS.get(admin_user_id)
        if (
            not session
            or now - session["updated_at"] > BATCH_RESET_SESSION_TTL
            or session["chat_id"] != str(chat_id)
            or session["message_id"] != message_id
        ):
            _BATCH_RESET_SESSIONS.pop(admin_user_id, None)
            return None
        session["selected"].intersection_update(valid_user_ids)
        if operation == "toggle":
            if target_user_id not in valid_user_ids:
                return None
            if target_user_id in session["selected"]:
                session["selected"].remove(target_user_id)
            else:
                session["selected"].add(target_user_id)
        elif operation == "all":
            session["selected"] = set(valid_user_ids)
        elif operation == "clear":
            session["selected"].clear()
        elif operation != "keep":
            raise ValueError("Invalid batch reset selection operation")
        session["updated_at"] = now
        return set(session["selected"])


def finish_batch_reset_session(admin_user_id, chat_id, message_id, valid_user_ids=None, now=None):
    now = time.monotonic() if now is None else now
    with _BATCH_RESET_LOCK:
        admin_user_id = str(admin_user_id)
        session = _BATCH_RESET_SESSIONS.get(admin_user_id)
        if (
            not session
            or now - session["updated_at"] > BATCH_RESET_SESSION_TTL
            or session["chat_id"] != str(chat_id)
            or session["message_id"] != message_id
        ):
            _BATCH_RESET_SESSIONS.pop(admin_user_id, None)
            return None
        _BATCH_RESET_SESSIONS.pop(admin_user_id, None)
        selected = set(session["selected"])
        if valid_user_ids is not None:
            selected.intersection_update(valid_user_ids)
        return selected


def admin_keyboard(bindings, selected_user_id=None):
    buttons = []
    sorted_bindings = sorted(bindings.items(), key=lambda item: (item[1]["key_name"].casefold(), item[0]))
    for user_id, binding in sorted_bindings:
        key_name = binding["key_name"]
        button_text = key_name if len(key_name) <= 64 else key_name[:61] + "..."
        buttons.append({"text": button_text, "callback_data": f"usage:{user_id}"})
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.append([{"text": "📊 Key 总览", "callback_data": "overview:0"}])
    if reset_api_configured():
        rows.append([{
            "text": "⚠️ 批量重置速率限制",
            "callback_data": "batch_start:0",
        }])
        rows.append([{
            "text": "↩️ 回滚 Key 使用量",
            "callback_data": "rollback_start:0",
        }])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def overview_keyboard(bindings, page, total_pages):
    rows = [[{"text": "🔄 刷新总览", "callback_data": f"overview:{page}"}]]
    page_candidates = reset_candidates(bindings)[
        page * OVERVIEW_PAGE_SIZE:(page + 1) * OVERVIEW_PAGE_SIZE
    ]
    ip_buttons = []
    for target_user_id, binding in page_candidates:
        key_name = binding["key_name"]
        label = key_name if len(key_name) <= 60 else key_name[:57] + "..."
        ip_buttons.append({
            "text": f"🌐 {label}",
            "callback_data": f"ip_detail:{target_user_id}:0:{page}",
        })
    rows.extend([ip_buttons[index:index + 2] for index in range(0, len(ip_buttons), 2)])
    if total_pages > 1:
        previous_page = max(0, page - 1)
        next_page = min(total_pages - 1, page + 1)
        rows.append([
            {"text": "◀️", "callback_data": f"overview:{previous_page}"},
            {"text": f"{page + 1}/{total_pages}", "callback_data": f"overview:{page}"},
            {"text": "▶️", "callback_data": f"overview:{next_page}"},
        ])
    rows.append([{"text": "◀️ 返回", "callback_data": "overview_back:0"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def ip_history_keyboard(target_user_id, page, total_pages, overview_page):
    rows = []
    if total_pages > 1:
        rows.append([
            {
                "text": "◀️",
                "callback_data": f"ip_detail:{target_user_id}:{max(0, page - 1)}:{overview_page}",
            },
            {
                "text": f"{page + 1}/{total_pages}",
                "callback_data": f"ip_detail:{target_user_id}:{page}:{overview_page}",
            },
            {
                "text": "▶️",
                "callback_data": f"ip_detail:{target_user_id}:{min(total_pages - 1, page + 1)}:{overview_page}",
            },
        ])
    rows.extend([
        [{
            "text": "🔄 刷新",
            "callback_data": f"ip_detail:{target_user_id}:{page}:{overview_page}",
        }],
        [{"text": "◀️ 返回 Key 总览", "callback_data": f"overview:{overview_page}"}],
    ])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def rollback_key_keyboard(bindings):
    buttons = []
    for target_user_id, binding in reset_candidates(bindings):
        key_name = binding["key_name"]
        buttons.append({
            "text": key_name if len(key_name) <= 64 else key_name[:61] + "...",
            "callback_data": f"rollback_key:{target_user_id}",
        })
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    rows.append([{"text": "◀️ 返回", "callback_data": "rollback_start:0"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def rollback_mode_keyboard():
    return json.dumps({"inline_keyboard": [
        [{"text": "👤 回滚单个 Key", "callback_data": "rollback_single:0"}],
        [{"text": "👥 回滚所有绑定 Key", "callback_data": "rollback_all:0"}],
        [{"text": "◀️ 返回", "callback_data": "rollback_back:0"}],
    ]}, ensure_ascii=False)


def rollback_batch_keyboard(batches):
    rows = []
    for batch in batches:
        batch_id = batch.get("batch_id")
        if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{8,32}", batch_id):
            continue
        source = "自动" if batch.get("reset_source") == "auto" else "手动"
        rows.append([{
            "text": (
                f"{format_timestamp(batch.get('created_at'))}｜{source}｜"
                f"{num(batch.get('key_count'))} 个 Key"
            ),
            "callback_data": f"rollback_all_prompt:{batch_id}",
        }])
    rows.append([{"text": "◀️ 返回", "callback_data": "rollback_start:0"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def rollback_all_confirmation_keyboard(batch_id, key_count):
    return json.dumps({"inline_keyboard": [
        [{
            "text": f"✅ 确认回滚全部 {key_count} 个 Key",
            "callback_data": f"rollback_all_confirm:{batch_id}",
        }],
        [{"text": "◀️ 返回版本列表", "callback_data": "rollback_all:0"}],
    ]}, ensure_ascii=False)


def rollback_backup_keyboard(target_user_id, backups):
    rows = []
    for backup in backups:
        backup_id = backup.get("backup_id")
        if isinstance(backup_id, bool) or not isinstance(backup_id, int) or backup_id <= 0:
            continue
        source = "自动" if backup.get("reset_source") == "auto" else "手动"
        created_at = format_timestamp(backup.get("created_at"))
        rows.append([{
            "text": f"#{backup_id}｜{created_at}｜{source}",
            "callback_data": f"rollback_prompt:{target_user_id}:{backup_id}",
        }])
    rows.append([{"text": "◀️ 返回选择 Key", "callback_data": "rollback_single:0"}])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def rollback_confirmation_keyboard(target_user_id, backup_id):
    return json.dumps({"inline_keyboard": [
        [{
            "text": f"✅ 确认回滚到备份 #{backup_id}",
            "callback_data": f"rollback_confirm:{target_user_id}:{backup_id}",
        }],
        [{
            "text": "◀️ 返回备份列表",
            "callback_data": f"rollback_key:{target_user_id}",
        }],
    ]}, ensure_ascii=False)


def format_rollback_backup(key_name, backup):
    snapshot = backup.get("snapshot") or {}
    source = "自动重置" if backup.get("reset_source") == "auto" else "手动重置"
    return "\n".join([
        "⚠️ 确认完整回滚？",
        "",
        f"Key：{key_name}",
        f"备份：#{backup.get('backup_id')}｜{source}",
        f"备份时间：{format_timestamp(backup.get('created_at'))}",
        f"5 小时用量：{money(snapshot.get('usage_5h'))}",
        f"每日用量：{money(snapshot.get('usage_1d'))}",
        f"每周用量：{money(snapshot.get('usage_7d'))}",
        f"5 小时窗口：{format_timestamp(snapshot.get('window_5h_start'))}",
        f"每日窗口：{format_timestamp(snapshot.get('window_1d_start'))}",
        f"每周窗口：{format_timestamp(snapshot.get('window_7d_start'))}",
        "",
        "确认后将覆盖当前六个速率限制字段，并定向刷新该 Key 的缓存。",
    ])


def find_complete_backup_batch(bindings, batch_id):
    if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{8,32}", batch_id):
        raise ValueError("Invalid backup batch ID")
    data = query_rate_limit_backup_batches(bindings) or {}
    if data.get("error"):
        raise RuntimeError(f"Backup batch lookup failed: {data.get('error')}")
    for batch in data.get("batches") or []:
        if batch.get("batch_id") == batch_id:
            return batch
    raise RuntimeError("Backup batch is incomplete or no longer available")


def format_all_rollback_confirmation(batch):
    backups = batch.get("backups") or []
    source = "自动重置" if batch.get("reset_source") == "auto" else "手动重置"
    lines = [
        "⚠️ 确认回滚所有绑定 Key？",
        "",
        f"版本时间：{format_timestamp(batch.get('created_at'))}",
        f"备份来源：{source}",
        f"可回滚：{len(backups)} / {num(batch.get('key_count'))} 个 Key",
        "",
    ]
    lines.extend(f"• {backup.get('key_name') or '-'}" for backup in backups[:30])
    if len(backups) > 30:
        lines.append(f"• …另有 {len(backups) - 30} 个 Key")
    lines.extend([
        "",
        "将恢复每个 Key 的 5 小时、每日、每周用量及对应窗口时间。",
        "不会修改 quota_used，也不会删除历史用量记录。",
        "每个 Key 都会定向刷新 Redis 限速缓存。",
    ])
    return "\n".join(lines)


def format_reset_backup_snapshot(key_name, backup):
    snapshot = backup.get("snapshot") or {}
    lines = [f"🔑 {key_name}"]
    last_used_at = snapshot.get("last_used_at")
    lines.append(
        f"• 最后使用：{format_timestamp(last_used_at) if last_used_at else '暂无使用记录'}"
    )
    append_limit(
        lines,
        "每周额度",
        snapshot.get("rate_limit_7d"),
        snapshot.get("usage_7d"),
    )
    return "\n".join(lines)


def format_reset_backup_snapshots(results):
    snapshots = [
        format_reset_backup_snapshot(result["key_name"], result["backup"])
        for result in results
        if isinstance(result.get("backup"), dict)
    ]
    return "\n\n".join(snapshots)


def tg(method, params=None, timeout=10):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    req = urllib.request.Request(f"{API}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API error: {payload}")
    return payload.get("result")


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ResetAfterBackupError(RuntimeError):
    def __init__(self, backup):
        super().__init__("Reset failed after backup")
        self.backup = backup


class ResetWindowAlignmentError(RuntimeError):
    def __init__(self, backup):
        super().__init__("Reset succeeded but window alignment failed")
        self.backup = backup


def reset_api_configured():
    return bool(SUB2API_BASE_URL and SUB2API_ADMIN_API_KEY)


def reset_key_rate_limit_usage(key_id):
    if isinstance(key_id, bool) or not isinstance(key_id, int) or key_id <= 0:
        raise ValueError("Invalid API key ID")
    if not reset_api_configured():
        raise RuntimeError("Sub2API reset API is not configured")
    body = json.dumps({"reset_rate_limit_usage": True}).encode("utf-8")
    request = urllib.request.Request(
        f"{SUB2API_BASE_URL}/api/v1/admin/api-keys/{key_id}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": SUB2API_ADMIN_API_KEY,
        },
        method="PUT",
    )
    opener = urllib.request.build_opener(NoRedirectHandler())
    with opener.open(request, timeout=SUB2API_ADMIN_TIMEOUT) as response:
        response_body = response.read(1_048_577)
    if len(response_body) > 1_048_576:
        raise RuntimeError("Sub2API response is too large")
    return json.loads(response_body.decode("utf-8")) if response_body else None


def backup_and_reset_key(key_name, account_id, reset_source, batch_id, reset_at=None):
    with _RESET_OPERATION_LOCK:
        backup = create_rate_limit_backup(key_name, account_id, reset_source, batch_id) or {}
        if backup.get("error"):
            raise RuntimeError(f"Rate-limit backup failed: {backup.get('error')}")
        key_id = backup.get("key_id")
        backup_id = backup.get("backup_id")
        if (
            isinstance(key_id, bool) or not isinstance(key_id, int) or key_id <= 0
            or isinstance(backup_id, bool) or not isinstance(backup_id, int) or backup_id <= 0
        ):
            raise RuntimeError("Rate-limit backup did not return valid IDs")
        try:
            reset_key_rate_limit_usage(key_id)
        except Exception as error:
            raise ResetAfterBackupError(backup) from error
        try:
            aligned = set_rate_limit_window_starts(
                key_id,
                datetime.now(timezone.utc) if reset_at is None else reset_at,
            ) or {}
            if aligned.get("error"):
                raise RuntimeError(f"Window alignment failed: {aligned.get('error')}")
        except Exception as error:
            raise ResetWindowAlignmentError(backup) from error
        return backup


def reset_selected_keys(bindings, selected_user_ids, reset_at=None):
    selected_user_ids = set(selected_user_ids)
    batch_id = secrets.token_hex(8)
    reset_at = datetime.now(timezone.utc) if reset_at is None else reset_at
    results = []
    for target_user_id, binding in reset_candidates(bindings):
        if target_user_id not in selected_user_ids:
            continue
        key_name = binding["key_name"]
        reset_completed = False
        backup_completed = False
        try:
            backup = backup_and_reset_key(
                key_name, binding["account_id"], "manual", batch_id, reset_at
            )
            backup_completed = True
            reset_completed = True
            checked_data = query_key_usage(key_name, binding["account_id"]) or {}
            checked_key = checked_data.get("key") or {}
            if not checked_key:
                raise RuntimeError("Post-check did not return API key data")
            results.append({
                "key_name": key_name,
                "status": "success",
                "backup": backup,
                "detail": (
                    f"5h {checked_key.get('usage_5h', '?')} / "
                    f"日 {checked_key.get('usage_1d', '?')} / "
                    f"周 {checked_key.get('usage_7d', '?')} / "
                    f"备份 #{backup['backup_id']}"
                ),
            })
        except Exception as error:
            if isinstance(error, ResetAfterBackupError):
                backup_completed = True
            elif isinstance(error, ResetWindowAlignmentError):
                backup_completed = True
                reset_completed = True
                backup = error.backup
            log_failure(
                f"batch reset target={masked_id(target_user_id)} recheck={str(reset_completed).lower()}",
                error,
            )
            result = {
                "key_name": key_name,
                "status": "warning" if reset_completed else "failed",
                "detail": (
                    "重置成功且已有备份，但复查失败"
                    if reset_completed and not isinstance(error, ResetWindowAlignmentError) else
                    "重置成功且已有备份，但统一窗口时间失败"
                    if isinstance(error, ResetWindowAlignmentError) else
                    "已备份，但重置失败" if backup_completed else
                    "备份失败，未执行重置"
                ),
            }
            if reset_completed:
                result["backup"] = backup
            results.append(result)
    return results


def format_batch_reset_results(results, config):
    success_count = sum(result["status"] == "success" for result in results)
    warning_count = sum(result["status"] == "warning" for result in results)
    failed_count = sum(result["status"] == "failed" for result in results)
    lines = [
        "✅ 批量重置完成",
        "",
        f"总计：{len(results)}",
        f"成功：{success_count}",
        f"需复查：{warning_count}",
        f"失败：{failed_count}",
        "",
    ]
    icons = {"success": "✅", "warning": "⚠️", "failed": "❌"}
    displayed_results = sorted(
        results,
        key=lambda result: ({"failed": 0, "warning": 1, "success": 2}[result["status"]], result["key_name"].casefold()),
    )[:30]
    for result in displayed_results:
        lines.append(f"{icons[result['status']]} {result['key_name']}：{result['detail']}")
    if len(results) > len(displayed_results):
        lines.append(f"…另有 {len(results) - len(displayed_results)} 个结果未展开，请单独查询确认。")
    lines.extend(["", f"🔄 复查时间：{format_refresh_timestamp(config)}"])
    return "\n".join(lines)


def _run_psql_json(sql, variables=None, allow_write=False):
    cmd = [PSQL_BIN, "-X", "--set=ON_ERROR_STOP=1", "-tAX"]
    if allow_write:
        cmd.append("--quiet")
    for name, value in sorted((variables or {}).items()):
        if not PSQL_VARIABLE_RE.fullmatch(name):
            raise ValueError("Invalid psql variable name")
        cmd.append(f"--set={name}={value}")
    # psql does not interpolate :'name' variables in text passed with --command.
    # Reading the fixed query from stdin keeps values in psql variables while
    # ensuring interpolation happens before PostgreSQL receives the statement.
    cmd.extend(["--file", "-"])
    env = {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PGAPPNAME": "sub2api-tg-bot",
        "PGCLIENTENCODING": "UTF8",
        "PGCONNECT_TIMEOUT": "5",
        "PGDATABASE": PGDATABASE,
        "PGHOST": PGHOST,
        "PGOPTIONS": (
            "-c statement_timeout=10000 -c lock_timeout=2000"
            if allow_write
            else "-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000"
        ),
        "PGPASSFILE": "/dev/null",
        "PGPASSWORD": PGPASSWORD,
        "PGPORT": PGPORT,
        "PGSSLMODE": PGSSLMODE,
        "PGUSER": PGUSER,
    }
    out = subprocess.check_output(
        cmd,
        input=("SET default_transaction_read_only=off;\n" + sql) if allow_write else sql,
        env=env,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
        timeout=12,
    ).strip()
    if not out:
        return None
    return json.loads(out)


def run_psql_json(sql, variables=None):
    return _run_psql_json(sql, variables, allow_write=False)


def run_psql_write_json(sql, variables=None):
    return _run_psql_json(sql, variables, allow_write=True)


def dec(v):
    if v is None:
        return Decimal("0")
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return Decimal("0")


def money(v):
    d = dec(v)
    s = f"{d:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def num(v):
    return str(int(v or 0))


def format_tokens(v):
    value = max(dec(v), Decimal("0"))
    if value >= Decimal("1000000000"):
        scaled = value / Decimal("1000000000")
        suffix = "B"
        decimals = 2
    elif value >= Decimal("1000000"):
        scaled = value / Decimal("1000000")
        suffix = "M"
        decimals = 2
    else:
        scaled = value / Decimal("1000")
        suffix = "k"
        decimals = 1 if value >= Decimal("1000") else 2
        if scaled < Decimal("0.01"):
            decimals = 3
    text = f"{scaled:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{text or '0'}{suffix}"


def progress_bar(used, total, width=12):
    used = max(dec(used), Decimal("0"))
    total = dec(total)
    if total <= 0:
        return ""
    ratio = min(used / total, Decimal("1"))
    filled = min(width, max(0, int(ratio * width + Decimal("0.5"))))
    return f"[{'█' * filled}{'░' * (width - filled)}] {money(ratio * 100)}%"


def format_timestamp(value):
    if not value:
        return "-"
    text = str(value)
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(ZoneInfo("Asia/Shanghai"))
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def format_date_range(start, end):
    if not start or not end:
        return None
    return f"{format_timestamp(start)[:10]} ～ {format_timestamp(end)[:10]}"


def format_refresh_timestamp(config):
    timezone_name = config.get("timezone") or "Asia/Shanghai"
    try:
        timezone = ZoneInfo(str(timezone_name))
    except (KeyError, TypeError, ValueError):
        timezone = ZoneInfo("Asia/Shanghai")
    return datetime.now(timezone).strftime("%Y-%m-%d %H:%M:%S")


def format_status(value):
    status = str(value or "-")
    return {
        "active": "正常",
        "disabled": "禁用",
        "expired": "已过期",
    }.get(status.lower(), status)


def cache_percentage_text(values):
    values = values or {}
    input_tokens = max(dec(values.get("input_tokens")), Decimal("0"))
    cache_creation = max(dec(values.get("cache_creation_tokens")), Decimal("0"))
    cache_read = max(dec(values.get("cache_read_tokens")), Decimal("0"))
    total_input = input_tokens + cache_creation + cache_read
    if total_input <= 0:
        return "暂无数据"
    read_percent = cache_read / total_input * 100
    creation_percent = cache_creation / total_input * 100
    return f"读取 {money(read_percent)}%｜写入 {money(creation_percent)}%"


def append_limit(lines, label, limit_value, used_value):
    limit = dec(limit_value)
    used = dec(used_value)
    if limit <= 0:
        lines.append(f"• {label}：不限（已用 {money(used)}）")
        return
    remaining = max(limit - used, Decimal("0"))
    summary = f"• {label}：已用 {money(used)} / 限额 {money(limit)} / 剩余 {money(remaining)}"
    if used > limit:
        summary += f" / 超出 {money(used - limit)}"
    lines.extend([summary, f"  {progress_bar(used, limit)}"])


def parse_upstream_timestamp(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float, Decimal)) or re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
            epoch = float(value)
            if epoch > 10_000_000_000:
                epoch /= 1000
            return datetime.fromtimestamp(epoch, timezone.utc)
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def reset_remaining_text(value, now=None):
    reset_at = parse_upstream_timestamp(value)
    if reset_at is None:
        return None
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seconds = int((reset_at - current).total_seconds())
    if seconds <= 0:
        return "等待快照更新"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d" + (f" {hours}h" if hours else "")
    if hours:
        return f"{hours}h" + (f" {minutes}m" if minutes else "")
    return f"{max(1, minutes)}m"


def format_key_expiry(value, now=None):
    if value is None or (isinstance(value, str) and not value.strip()):
        return "永不过期"
    expires_at = parse_upstream_timestamp(value)
    if expires_at is None:
        return "数据不可用"
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    timestamp = expires_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    seconds = int((expires_at - current).total_seconds())
    if seconds <= 0:
        return f"{timestamp}｜已过期"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        remaining = f"{days}d" + (f" {hours}h" if hours else "")
    elif hours:
        remaining = f"{hours}h" + (f" {minutes}m" if minutes else "")
    else:
        remaining = f"{max(1, minutes)}m"
    return f"{timestamp}｜剩余：{remaining}"


def append_reset_time(lines, reset_at, now=None, label="重置时间", indent="  "):
    remaining = reset_remaining_text(reset_at, now)
    if remaining:
        timestamp = parse_upstream_timestamp(reset_at).astimezone(ZoneInfo("Asia/Shanghai"))
        lines.append(
            f"{indent}{label}：{timestamp.strftime('%Y-%m-%d %H:%M:%S')}｜剩余：{remaining}"
        )


def append_model_section(lines, title, models):
    lines.extend(["", title])
    if not models:
        lines.append("• 暂无使用记录")
        return
    for index, model in enumerate(models):
        if index:
            lines.append("")
        lines.extend([
            f"• {model.get('model') or '-'}：{num(model.get('requests'))} 次｜费用 {money(model.get('actual_cost'))}",
            f"  Tokens：输入 {format_tokens(model.get('input_tokens'))} / 输出 {format_tokens(model.get('output_tokens'))}",
            f"  缓存占比：{cache_percentage_text(model)}",
        ])


def query_key_usage(key_name, account_id=None):
    if not isinstance(key_name, str) or not KEY_NAME_RE.fullmatch(key_name):
        raise ValueError("Invalid key name in binding config")
    if account_id is not None and (isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0):
        raise ValueError("Invalid account ID in binding config")
    if account_id is None:
        sql = "SELECT sub2api_tg_bot_api.usage(:'key_name')::text;"
        return run_psql_json(sql, {"key_name": key_name})
    sql = "SELECT sub2api_tg_bot_api.usage_with_account(:'key_name', :'account_id'::bigint)::text;"
    return run_psql_json(sql, {"key_name": key_name, "account_id": str(account_id)})


def query_key_ip_history(key_name, page=0, page_size=IP_HISTORY_PAGE_SIZE):
    if not isinstance(key_name, str) or not KEY_NAME_RE.fullmatch(key_name):
        raise ValueError("Invalid key name")
    if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 1000:
        raise ValueError("Invalid IP history page")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 20
    ):
        raise ValueError("Invalid IP history page size")
    sql = (
        "SELECT sub2api_tg_bot_api.key_ip_history("
        ":'key_name', :'offset'::integer, :'limit'::integer)::text;"
    )
    return run_psql_json(sql, {
        "key_name": key_name,
        "offset": str(page * page_size),
        "limit": str(page_size),
    })


def query_key_overview(key_name):
    if not isinstance(key_name, str) or not KEY_NAME_RE.fullmatch(key_name):
        raise ValueError("Invalid key name")
    sql = "SELECT sub2api_tg_bot_api.key_overview(:'key_name')::text;"
    return run_psql_json(sql, {"key_name": key_name})


def query_account_estimate(account_id):
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        raise ValueError("Invalid account ID in binding config")
    sql = "SELECT sub2api_tg_bot_api.account_estimate(:'account_id'::bigint)::text;"
    return run_psql_json(sql, {"account_id": str(account_id)})


def create_rate_limit_backup(key_name, account_id, reset_source, batch_id):
    if not isinstance(key_name, str) or not KEY_NAME_RE.fullmatch(key_name):
        raise ValueError("Invalid key name")
    if account_id is not None and (
        isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0
    ):
        raise ValueError("Invalid account ID")
    if reset_source not in {"manual", "auto"}:
        raise ValueError("Invalid reset source")
    if not isinstance(batch_id, str) or not re.fullmatch(r"[0-9a-f]{8,32}", batch_id):
        raise ValueError("Invalid backup batch ID")
    sql = (
        "SELECT sub2api_tg_bot_api.backup_rate_limits("
        ":'key_name', NULLIF(:'account_id', '')::bigint, "
        ":'reset_source', :'batch_id')::text;"
    )
    return run_psql_write_json(sql, {
        "key_name": key_name,
        "account_id": "" if account_id is None else str(account_id),
        "reset_source": reset_source,
        "batch_id": batch_id,
    })


def set_rate_limit_window_starts(key_id, reset_at):
    if isinstance(key_id, bool) or not isinstance(key_id, int) or key_id <= 0:
        raise ValueError("Invalid API key ID")
    if not isinstance(reset_at, datetime):
        raise ValueError("Invalid reset time")
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    sql = (
        "SELECT sub2api_tg_bot_api.set_rate_limit_window_starts("
        ":'key_id'::bigint, :'reset_at'::timestamptz)::text;"
    )
    return run_psql_write_json(sql, {
        "key_id": str(key_id),
        "reset_at": reset_at.astimezone(timezone.utc).isoformat(),
    })


def query_rate_limit_backup_batches(bindings):
    key_names = [binding["key_name"] for _user_id, binding in reset_candidates(bindings)]
    if not key_names:
        return {"batches": []}
    sql = (
        "SELECT sub2api_tg_bot_api.rate_limit_backup_batches("
        ":'key_names'::jsonb)::text;"
    )
    return run_psql_json(sql, {"key_names": json.dumps(key_names, ensure_ascii=False)})


def query_rate_limit_backups(key_name):
    if not isinstance(key_name, str) or not KEY_NAME_RE.fullmatch(key_name):
        raise ValueError("Invalid key name")
    sql = "SELECT sub2api_tg_bot_api.rate_limit_backups(:'key_name')::text;"
    return run_psql_json(sql, {"key_name": key_name})


def restore_rate_limit_backup(backup_id, key_name):
    if isinstance(backup_id, bool) or not isinstance(backup_id, int) or backup_id <= 0:
        raise ValueError("Invalid backup ID")
    if not isinstance(key_name, str) or not KEY_NAME_RE.fullmatch(key_name):
        raise ValueError("Invalid key name")
    sql = (
        "SELECT sub2api_tg_bot_api.restore_rate_limit_backup("
        ":'backup_id'::bigint, :'key_name')::text;"
    )
    return run_psql_write_json(sql, {"backup_id": str(backup_id), "key_name": key_name})


def find_rate_limit_backup(key_name, backup_id):
    data = query_rate_limit_backups(key_name) or {}
    if data.get("error"):
        raise RuntimeError(f"Backup lookup failed: {data.get('error')}")
    for backup in data.get("backups") or []:
        if backup.get("backup_id") == backup_id:
            return data.get("key_id"), backup
    raise RuntimeError("Backup does not belong to this configured key")


def rollback_key_rate_limits(key_name, account_id, backup_id):
    with _RESET_OPERATION_LOCK:
        key_id, backup = find_rate_limit_backup(key_name, backup_id)
        return _rollback_key_rate_limits_unlocked(
            key_name, account_id, key_id, backup_id, backup
        )


def _rollback_key_rate_limits_unlocked(key_name, account_id, key_id, backup_id, backup):
    if isinstance(key_id, bool) or not isinstance(key_id, int) or key_id <= 0:
        raise RuntimeError("Backup lookup did not return a valid key ID")
    # The official reset invalidates Sub2API's targeted Redis rate-limit cache.
    # Restoring afterwards makes the next request reload all restored values.
    reset_key_rate_limit_usage(key_id)
    restored = restore_rate_limit_backup(backup_id, key_name) or {}
    if restored.get("error"):
        raise RuntimeError(f"Backup restore failed: {restored.get('error')}")
    checked = query_key_usage(key_name, account_id) or {}
    checked_key = checked.get("key") or {}
    if not checked_key:
        raise RuntimeError("Rollback completed but post-check failed")
    return {"backup": backup, "restored": restored, "key": checked_key}


def rollback_all_key_rate_limits(bindings, batch_id):
    with _RESET_OPERATION_LOCK:
        batch = find_complete_backup_batch(bindings, batch_id)
        bindings_by_name = {
            binding["key_name"]: binding
            for _user_id, binding in reset_candidates(bindings)
        }
        prepared = []
        for backup in batch.get("backups") or []:
            key_name = backup.get("key_name")
            binding = bindings_by_name.get(key_name)
            backup_id = backup.get("backup_id")
            if not binding:
                raise RuntimeError("Configured key is missing")
            key_id, current_backup = find_rate_limit_backup(key_name, backup_id)
            prepared.append((key_name, binding, backup_id, key_id, current_backup))
        results = []
        for key_name, binding, backup_id, key_id, current_backup in prepared:
            try:
                _rollback_key_rate_limits_unlocked(
                    key_name, binding["account_id"], key_id, backup_id, current_backup
                )
                results.append({"key_name": key_name, "status": "success"})
            except Exception as error:
                log_failure(f"rollback all key={key_name}", error)
                results.append({"key_name": key_name or "-", "status": "failed"})
        return batch, results


def format_all_rollback_results(batch, results):
    success_count = sum(result.get("status") == "success" for result in results)
    failed_count = len(results) - success_count
    lines = [
        "✅ 全员回滚完成" if failed_count == 0 else "⚠️ 全员回滚完成，部分 Key 失败",
        "",
        f"版本时间：{format_timestamp(batch.get('created_at'))}",
        f"成功：{success_count}",
        f"失败：{failed_count}",
    ]
    for result in results:
        if result.get("status") != "success":
            lines.append(f"❌ {result.get('key_name')}")
    return "\n".join(lines)


def collect_key_overview(bindings):
    overview = []
    for target_user_id, binding in reset_candidates(bindings):
        key_name = binding["key_name"]
        try:
            data = query_key_overview(key_name) or {}
            key = data.get("key") or {}
            if not key:
                raise RuntimeError("Overview query did not return API key data")
            overview.append({
                "key_name": key_name,
                "last_used_at": key.get("last_used_at"),
                "rate_limit_7d": key.get("rate_limit_7d"),
                "usage_7d": key.get("usage_7d"),
                "window_7d_end": key.get("window_7d_end"),
                "today_ip_count": (data.get("ip_counts") or {}).get("today"),
                "yesterday_ip_count": (data.get("ip_counts") or {}).get("yesterday"),
            })
        except Exception as error:
            log_failure(f"overview target={masked_id(target_user_id)}", error)
            overview.append({"key_name": key_name, "error": True})
    return overview


def collect_account_overview(bindings):
    account_bindings = {}
    for _target_user_id, binding in sorted(
        bindings.items(), key=lambda item: (item[1]["key_name"].casefold(), item[0])
    ):
        account_id = binding["account_id"]
        if account_id is None:
            continue
        key_names = account_bindings.setdefault(account_id, [])
        if binding["key_name"] not in key_names:
            key_names.append(binding["key_name"])

    overview = []
    for account_id, key_names in account_bindings.items():
        try:
            data = query_account_estimate(account_id) or {}
            if data.get("error"):
                raise RuntimeError("Account estimate query did not return account data")
            overview.append({
                "account_id": account_id,
                "account_name": data.get("name"),
                "key_names": key_names,
                "used_7d_percent": data.get("used_7d_percent"),
                "consumed_amount": data.get("consumed_amount"),
                "snapshot_updated_at": data.get("snapshot_updated_at"),
                "reset_7d_at": data.get("window_end"),
            })
        except Exception as error:
            log_failure(f"account overview account={masked_id(account_id)}", error)
            overview.append({"account_id": account_id, "key_names": key_names, "error": True})
    return overview


def mask_account_name(value):
    name = str(value or "").strip()
    if "@" not in name:
        return name or "未命名账号"
    local, domain = name.rsplit("@", 1)
    if not local or not domain:
        return name
    return f"{local[:3]}***@{domain}"


def account_estimated_total(consumed_amount, used_percent):
    if consumed_amount is None or used_percent is None:
        return None
    percent = dec(used_percent)
    if percent <= 0:
        return None
    return max(dec(consumed_amount), Decimal("0")) * Decimal("100") / percent


def append_account_overview(lines, accounts, now=None):
    lines.extend(["", "🌐 上游账号信息"])
    if not accounts:
        lines.append("• 暂无配置了 account_id 的绑定账号。")
        return

    total_consumed = Decimal("0")
    total_estimated = Decimal("0")
    consumed_count = 0
    estimated_count = 0
    for account in accounts:
        lines.extend(["", f"👤 {mask_account_name(account.get('account_name'))}"])
        lines.append(f"• 绑定 Key：{'、'.join(account.get('key_names') or []) or '-'}")
        if account.get("error"):
            lines.extend([
                "• 账号使用：数据不可用",
                "• 已消耗金额：数据不可用",
                "• 预估总金额：数据不可用",
            ])
            continue

        used_percent = account.get("used_7d_percent")
        consumed_amount = account.get("consumed_amount")
        if used_percent is None:
            lines.append("• 账号使用：暂无数据")
        else:
            percent = max(dec(used_percent), Decimal("0"))
            lines.extend([
                f"• 账号使用：{money(percent)}%",
                f"  {progress_bar(percent, 100)}",
            ])
        append_reset_time(
            lines,
            account.get("reset_7d_at"),
            now,
            label="上游 7 日重置时间",
            indent="• ",
        )
        if consumed_amount is None:
            lines.append("• 已消耗金额：暂无数据")
        else:
            consumed = max(dec(consumed_amount), Decimal("0"))
            total_consumed += consumed
            consumed_count += 1
            lines.append(f"• 已消耗金额：${money(consumed)}")
        estimated = account_estimated_total(consumed_amount, used_percent)
        if estimated is None:
            lines.append("• 预估总金额：暂无数据")
        else:
            total_estimated += estimated
            estimated_count += 1
            lines.append(f"• 预估总金额：约 ${money(estimated)}")

    lines.extend(["", "📦 全部账号汇总"])
    lines.append(
        f"• 已消耗金额：${money(total_consumed)}"
        if consumed_count == len(accounts) else "• 已消耗金额：数据不完整"
    )
    lines.append(
        f"• 预估总金额：约 ${money(total_estimated)}"
        if estimated_count == len(accounts) else "• 预估总金额：数据不完整"
    )


def format_key_overview(overview, page=0, page_size=OVERVIEW_PAGE_SIZE, accounts=None, now=None):
    if isinstance(page, bool) or not isinstance(page, int):
        raise ValueError("Invalid overview page")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise ValueError("Invalid overview page size")
    total_pages = max(1, (len(overview) + page_size - 1) // page_size)
    page = min(max(page, 0), total_pages - 1)
    page_items = overview[page * page_size:(page + 1) * page_size]
    lines = ["📊 Key 总览"]
    if not page_items:
        lines.extend(["", "暂无已绑定 Key。"])
    for item in page_items:
        lines.extend(["", f"🔑 {item['key_name']}"])
        if item.get("error"):
            lines.extend([
                "• 最后使用：数据不可用",
                "• 每周额度：数据不可用",
                "• 去重 IP：数据不可用",
            ])
            continue
        last_used_at = item.get("last_used_at")
        lines.append(
            f"• 最后使用：{format_timestamp(last_used_at) if last_used_at else '暂无使用记录'}"
        )
        append_limit(lines, "每周额度", item.get("rate_limit_7d"), item.get("usage_7d"))
        if dec(item.get("rate_limit_7d")) > 0:
            append_reset_time(lines, item.get("window_7d_end"), now)
        lines.append(
            f"• 去重 IP：今日 {num(item.get('today_ip_count'))}｜"
            f"昨日 {num(item.get('yesterday_ip_count'))}"
        )
    append_account_overview(lines, accounts or [], now)
    return "\n".join(lines), page, total_pages


def format_key_ip_history(key_name, data, page=0, page_size=IP_HISTORY_PAGE_SIZE):
    if data.get("error"):
        raise RuntimeError(f"IP history query failed: {data.get('error')}")
    total = data.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise RuntimeError("IP history query did not return a valid total")
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(page, 0), total_pages - 1)
    lines = [
        f"🌐 {key_name}｜近 3 日去重 IP",
        "",
        f"统计范围：{format_timestamp(data.get('window_start'))} 至当前",
        f"去重 IP：{total} 个",
    ]
    ips = data.get("ips") or []
    if not ips:
        lines.extend(["", "暂无 IP 记录。"])
    for index, item in enumerate(ips, start=page * page_size + 1):
        lines.extend([
            "",
            f"{index}. {item.get('ip_address') or '-'}",
            f"   • 首次：{format_timestamp(item.get('first_seen'))}",
            f"   • 最近：{format_timestamp(item.get('last_seen'))}",
            f"   • 请求：{num(item.get('requests'))} 次",
        ])
    return "\n".join(lines), page, total_pages


def load_alert_state():
    try:
        with open(ALERT_STATE_PATH, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log_failure("alert state load", e)
        return {}


def save_alert_state(state):
    tmp = ALERT_STATE_PATH + ".tmp"
    os.makedirs(os.path.dirname(ALERT_STATE_PATH) or ".", exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, ALERT_STATE_PATH)
    os.chmod(ALERT_STATE_PATH, 0o600)


def notify_admins(admins, text):
    delivered = 0
    for admin_user_id in sorted(admins):
        try:
            tg("sendMessage", {"chat_id": admin_user_id, "text": text})
            delivered += 1
        except Exception as error:
            log_failure(f"notify admin={masked_id(admin_user_id)}", error)
    return delivered


def check_weekly_alerts():
    try:
        cfg = load_config()
        bindings = config_bindings(cfg)
        state = load_alert_state()
        changed = False
        for user_id, binding in bindings.items():
            try:
                key_name = binding["key_name"]
                data = query_key_usage(key_name, binding["account_id"]) or {}
                key = data.get("key") or {}
                limit = dec(key.get("rate_limit_7d"))
                used = dec(key.get("usage_7d"))
                if limit <= 0:
                    continue
                remaining = limit - used
                ratio = remaining / limit
                window = str(key.get("window_7d_start") or "unknown")
                state_key = f"{user_id}:{key_name}:{window}"
                if ratio <= Decimal("0.20") and state_key not in state:
                    text = (
                        f"⚠️ 周限额提醒\n"
                        f"Key：{key_name}\n"
                        f"周限额：{money(limit)}\n"
                        f"已用：{money(used)}\n"
                        f"剩余：{money(max(remaining, Decimal('0')))}（{money(max(ratio, Decimal('0')) * 100)}%）"
                    )
                    tg("sendMessage", {"chat_id": user_id, "text": text})
                    state[state_key] = {"alerted_at": time.time()}
                    changed = True
                    print(f"weekly alert sent user={masked_id(user_id)} remaining={ratio:.4f}", flush=True)
            except Exception as e:
                log_failure(f"weekly alert check user={masked_id(user_id)}", e)
        if changed:
            save_alert_state(state)
    except Exception as e:
        log_failure("weekly alert scan", e)


def alert_loop():
    time.sleep(10)
    while True:
        check_weekly_alerts()
        time.sleep(max(ALERT_CHECK_INTERVAL, 60))


def format_usage(key_name, data, now=None):
    if data.get("error") == "duplicate_key_name":
        return (
            f"⚠️ 检测到多个同名 Key：{key_name}\n\n"
            "为避免显示错误用户的数据，请先在 Sub2API 中将 Key 名称修改为唯一名称。"
        )
    k = data.get("key")
    if not k:
        return f"未找到绑定的 key：{key_name}"
    seven_days = data.get("seven_days") or {}
    today = data.get("today") or {}
    models_today = data.get("models_today") or []
    models_7d = data.get("models_7d") or []
    lines = [
        f"🔑 Key：{k.get('name')}",
        f"📅 到期时间：{format_key_expiry(k.get('expires_at'), now)}",
        f"状态：{format_status(k.get('status'))}",
        "",
        "⏱ 限额",
    ]
    append_limit(lines, "5 小时", k.get("rate_limit_5h"), k.get("usage_5h"))
    if dec(k.get("rate_limit_5h")) > 0:
        append_reset_time(lines, k.get("window_5h_end"), now)
    append_limit(lines, "每日", k.get("rate_limit_1d"), k.get("usage_1d"))
    append_limit(lines, "每周", k.get("rate_limit_7d"), k.get("usage_7d"))
    if dec(k.get("rate_limit_7d")) > 0:
        append_reset_time(lines, k.get("window_7d_end"), now)
    lines.extend([
        "",
        "📅 今日用量",
        f"• 请求：{num(today.get('requests'))}",
        f"• Tokens：输入 {format_tokens(today.get('input_tokens'))} / 输出 {format_tokens(today.get('output_tokens'))}",
        f"• 缓存占比：{cache_percentage_text(today)}",
        f"• 费用：{money(today.get('actual_cost'))}",
    ])
    append_model_section(lines, "🤖 今日模型 Top 5", models_today)
    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━",
        "",
        "📊 7天用量",
    ])
    seven_days_range = format_date_range(seven_days.get("window_start"), seven_days.get("window_end"))
    if seven_days_range:
        lines.append(f"统计范围：{seven_days_range}")
    lines.extend([
        f"• 请求：{num(seven_days.get('requests'))}",
        f"• Tokens：输入 {format_tokens(seven_days.get('input_tokens'))} / 输出 {format_tokens(seven_days.get('output_tokens'))}",
        f"• 缓存占比：{cache_percentage_text(seven_days)}",
        f"• 费用：{money(seven_days.get('actual_cost'))}",
    ])
    if k.get("last_used_at"):
        lines.append(f"• 最近使用：{format_timestamp(k.get('last_used_at'))}")
    append_model_section(lines, "🤖 7天模型 Top 5", models_7d)
    return "\n".join(lines)


def is_private_user_chat(chat, user):
    chat_id = chat.get("id")
    user_id = user.get("id")
    return chat.get("type") == "private" and chat_id is not None and str(chat_id) == str(user_id)


def allow_check(user_id, now=None, cooldown=None):
    now = time.monotonic() if now is None else now
    cooldown = CHECK_COOLDOWN if cooldown is None else cooldown
    with _RATE_LIMIT_LOCK:
        last = _LAST_CHECK_BY_USER.get(user_id)
        if last is not None and now - last < cooldown:
            return False, max(1, int(cooldown - (now - last) + 0.999))
        _LAST_CHECK_BY_USER[user_id] = now
        if len(_LAST_CHECK_BY_USER) > 4096:
            cutoff = now - max(max(CHECK_COOLDOWN, ADMIN_CHECK_COOLDOWN) * 2, 60)
            stale = [key for key, value in _LAST_CHECK_BY_USER.items() if value < cutoff]
            for key in stale:
                _LAST_CHECK_BY_USER.pop(key, None)
        return True, 0


def handle_message(msg):
    chat = msg.get("chat", {})
    user = msg.get("from", {})
    text_in = (msg.get("text") or "").strip()
    chat_id = chat.get("id")
    user_id = str(user.get("id"))
    if not chat_id or not text_in:
        return
    cmd = text_in.split()[0].split("@", 1)[0].lower()
    if cmd not in ("/start", "/check"):
        return
    if not is_private_user_chat(chat, user):
        tg("sendMessage", {"chat_id": chat_id, "text": "为保护用量信息，请私聊机器人查询。"})
        return
    if cmd == "/start":
        tg("sendMessage", {"chat_id": chat_id, "text": "发送 /check 查询你绑定的 Sub2API key 用量。"})
        return
    cfg = load_config()
    bindings = config_bindings(cfg)
    if user_id in config_admins(cfg):
        if not bindings:
            tg("sendMessage", {"chat_id": chat_id, "text": "当前没有配置任何用户绑定。"})
            return
        tg("sendMessage", {
            "chat_id": chat_id,
            "text": "请选择要查看的 Key：",
            "reply_markup": admin_keyboard(bindings),
        })
        return
    allowed, retry_after = allow_check(user_id)
    if not allowed:
        tg("sendMessage", {"chat_id": chat_id, "text": f"查询过于频繁，请 {retry_after} 秒后再试。"})
        return
    binding = bindings.get(user_id)
    if not binding:
        tg("sendMessage", {"chat_id": chat_id, "text": "你的 Telegram ID 还没有绑定 key。"})
        return
    key_name = binding["key_name"]
    t0 = time.perf_counter()
    try:
        data = query_key_usage(key_name, binding["account_id"])
        t1 = time.perf_counter()
        reply = format_usage(key_name, data or {})
        t2 = time.perf_counter()
        tg("sendMessage", {"chat_id": chat_id, "text": reply})
        t3 = time.perf_counter()
        print(f"check completed user={masked_id(user_id)} query={t1-t0:.3f}s format={t2-t1:.3f}s send={t3-t2:.3f}s total={t3-t0:.3f}s", flush=True)
    except Exception as e:
        log_failure(f"check user={masked_id(user_id)}", e)
        tg("sendMessage", {"chat_id": chat_id, "text": "查询失败，请稍后再试。"})


def handle_callback_query(callback):
    callback_id = callback.get("id")
    callback_data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    user = callback.get("from") or {}
    user_id = str(user.get("id"))
    action, separator, target_user_id = callback_data.partition(":")
    if not callback_id or not separator or action not in {
        "usage", "overview", "overview_back", "ip_detail",
        "batch_start", "batch_toggle", "batch_all", "batch_clear",
        "batch_review", "batch_back", "batch_confirm", "batch_cancel",
        "reset_prompt", "reset_confirm", "reset_cancel",
        "rollback_start", "rollback_single", "rollback_key", "rollback_prompt",
        "rollback_confirm", "rollback_all", "rollback_all_prompt",
        "rollback_all_confirm", "rollback_back",
    }:
        return
    if not is_private_user_chat(chat, user):
        tg("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "为保护用量信息，请私聊机器人查询。",
            "show_alert": "true",
        })
        return
    try:
        cfg = load_config()
        bindings = config_bindings(cfg)
        if user_id not in config_admins(cfg):
            tg("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "你没有管理员权限。",
                "show_alert": "true",
            })
            return
        if action.startswith("rollback_"):
            chat_id = chat.get("id")
            message_id = message.get("message_id")
            if message_id is None:
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "该操作已失效，请重新发送 /check。",
                    "show_alert": "true",
                })
                return
            if action != "rollback_back" and not reset_api_configured():
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "回滚功能尚未配置。",
                    "show_alert": "true",
                })
                return
            if action == "rollback_back":
                tg("answerCallbackQuery", {"callback_query_id": callback_id})
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": "请选择要查看的 Key：",
                    "reply_markup": admin_keyboard(bindings),
                })
                return
            if action == "rollback_start":
                tg("answerCallbackQuery", {"callback_query_id": callback_id})
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": "↩️ Key 使用量回滚\n\n请选择回滚方式：",
                    "reply_markup": rollback_mode_keyboard(),
                })
                return
            if action == "rollback_single":
                tg("answerCallbackQuery", {"callback_query_id": callback_id})
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": "↩️ 请选择需要回滚的 Key：",
                    "reply_markup": rollback_key_keyboard(bindings),
                })
                return
            if action == "rollback_all":
                data = query_rate_limit_backup_batches(bindings) or {}
                if data.get("error"):
                    raise RuntimeError(f"Backup batch lookup failed: {data.get('error')}")
                batches = data.get("batches") or []
                tg("answerCallbackQuery", {"callback_query_id": callback_id})
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": (
                        "📦 选择全员回滚版本\n\n"
                        "只显示包含当前全部绑定 Key 的完整备份版本。"
                        if batches else
                        "📦 暂无可用的全员回滚版本。\n\n"
                        "旧备份和未覆盖全部绑定 Key 的批次仍可单 Key 回滚。"
                    ),
                    "reply_markup": rollback_batch_keyboard(batches),
                })
                return
            if action in {"rollback_all_prompt", "rollback_all_confirm"}:
                batch_id = target_user_id
                batch = find_complete_backup_batch(bindings, batch_id)
                key_count = len(batch.get("backups") or [])
                if action == "rollback_all_prompt":
                    tg("answerCallbackQuery", {"callback_query_id": callback_id})
                    tg("editMessageText", {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": format_all_rollback_confirmation(batch),
                        "reply_markup": rollback_all_confirmation_keyboard(
                            batch_id, key_count
                        ),
                    })
                    return
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": f"正在回滚全部 {key_count} 个 Key…",
                })
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": f"⏳ 正在回滚全部 {key_count} 个 Key，请稍候…",
                })
                batch, results = rollback_all_key_rate_limits(bindings, batch_id)
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": format_all_rollback_results(batch, results),
                    "reply_markup": admin_keyboard(bindings),
                })
                return
            rollback_user_id, backup_separator, backup_id_text = target_user_id.partition(":")
            if action == "rollback_key":
                rollback_user_id = target_user_id
            if (
                not rollback_user_id.isdigit()
                or (action != "rollback_key" and (
                    not backup_separator or not backup_id_text.isdigit()
                ))
            ):
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "回滚信息无效，请重新开始。",
                    "show_alert": "true",
                })
                return
            binding = bindings.get(rollback_user_id)
            if not binding:
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "该 Key 绑定已不存在。",
                    "show_alert": "true",
                })
                return
            key_name = binding["key_name"]
            if action == "rollback_key":
                data = query_rate_limit_backups(key_name) or {}
                backups = data.get("backups") or []
                tg("answerCallbackQuery", {"callback_query_id": callback_id})
                text = (
                    f"↩️ {key_name} 最近的回滚备份："
                    if backups else
                    f"↩️ {key_name} 暂无可回滚备份。"
                )
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "reply_markup": rollback_backup_keyboard(rollback_user_id, backups),
                })
                return
            backup_id = int(backup_id_text)
            if action == "rollback_prompt":
                _key_id, backup = find_rate_limit_backup(key_name, backup_id)
                tg("answerCallbackQuery", {"callback_query_id": callback_id})
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": format_rollback_backup(key_name, backup),
                    "reply_markup": rollback_confirmation_keyboard(rollback_user_id, backup_id),
                })
                return
            tg("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": f"正在回滚 {key_name}…",
            })
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f"⏳ 正在回滚 {key_name} 到备份 #{backup_id}，请稍候…",
            })
            result = rollback_key_rate_limits(
                key_name, binding["account_id"], backup_id,
            )
            checked_key = result["key"]
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": "\n".join([
                    "✅ Key 使用量已完整回滚",
                    f"Key：{key_name}",
                    f"备份：#{backup_id}",
                    f"5 小时：{money(checked_key.get('usage_5h'))}",
                    f"每日：{money(checked_key.get('usage_1d'))}",
                    f"每周：{money(checked_key.get('usage_7d'))}",
                    "Sub2API 缓存已定向失效。",
                ]),
                "reply_markup": admin_keyboard(bindings),
            })
            return
        if action == "overview_back":
            tg("answerCallbackQuery", {"callback_query_id": callback_id})
            tg("editMessageText", {
                "chat_id": chat.get("id"),
                "message_id": message.get("message_id"),
                "text": "请选择要查看的 Key：",
                "reply_markup": admin_keyboard(bindings),
            })
            return
        if action == "ip_detail":
            parts = target_user_id.split(":")
            if len(parts) != 3 or any(not part.isdigit() for part in parts):
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "IP 查询信息无效，请重新发送 /check。",
                    "show_alert": "true",
                })
                return
            ip_user_id, ip_page_text, overview_page_text = parts
            binding = bindings.get(ip_user_id)
            if not binding:
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "该 Key 绑定已不存在。",
                    "show_alert": "true",
                })
                return
            allowed, retry_after = allow_check(user_id, cooldown=ADMIN_CHECK_COOLDOWN)
            if not allowed:
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": f"查询过于频繁，请 {retry_after} 秒后再试。",
                    "show_alert": "true",
                })
                return
            requested_ip_page = int(ip_page_text)
            overview_page = int(overview_page_text)
            data = query_key_ip_history(binding["key_name"], requested_ip_page) or {}
            reply, ip_page, total_pages = format_key_ip_history(
                binding["key_name"], data, requested_ip_page
            )
            if ip_page != requested_ip_page:
                data = query_key_ip_history(binding["key_name"], ip_page) or {}
                reply, ip_page, total_pages = format_key_ip_history(
                    binding["key_name"], data, ip_page
                )
            reply += f"\n\n🔄 刷新时间：{format_refresh_timestamp(cfg)}"
            tg("answerCallbackQuery", {"callback_query_id": callback_id})
            tg("editMessageText", {
                "chat_id": chat.get("id"),
                "message_id": message.get("message_id"),
                "text": reply,
                "reply_markup": ip_history_keyboard(
                    ip_user_id, ip_page, total_pages, overview_page
                ),
            })
            return
        if action == "overview":
            if not target_user_id.isdigit():
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "总览页码无效，请重新发送 /check。",
                    "show_alert": "true",
                })
                return
            allowed, retry_after = allow_check(user_id, cooldown=ADMIN_CHECK_COOLDOWN)
            if not allowed:
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": f"查询过于频繁，请 {retry_after} 秒后再试。",
                    "show_alert": "true",
                })
                return
            tg("answerCallbackQuery", {"callback_query_id": callback_id})
            overview = collect_key_overview(bindings)
            accounts = collect_account_overview(bindings)
            reply, page, total_pages = format_key_overview(
                overview, int(target_user_id), accounts=accounts
            )
            reply += f"\n\n🔄 刷新时间：{format_refresh_timestamp(cfg)}"
            tg("editMessageText", {
                "chat_id": chat.get("id"),
                "message_id": message.get("message_id"),
                "text": reply,
                "reply_markup": overview_keyboard(bindings, page, total_pages),
            })
            return
        if action in {"reset_prompt", "reset_confirm", "reset_cancel"}:
            tg("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "重置入口已更新，请重新发送 /check。",
                "show_alert": "true",
            })
            return
        if action.startswith("batch_"):
            chat_id = chat.get("id")
            message_id = message.get("message_id")
            if message_id is None:
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "该操作已失效，请重新发送 /check。",
                    "show_alert": "true",
                })
                return
            if action != "batch_cancel" and not reset_api_configured():
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "重置功能尚未配置。",
                    "show_alert": "true",
                })
                return
            candidates = reset_candidates(bindings)
            valid_user_ids = {candidate_user_id for candidate_user_id, _binding in candidates}
            if action == "batch_start":
                if not candidates:
                    tg("answerCallbackQuery", {
                        "callback_query_id": callback_id,
                        "text": "当前没有可重置的 Key。",
                        "show_alert": "true",
                    })
                    return
                selected_user_ids = start_batch_reset_session(user_id, chat_id, message_id)
                tg("answerCallbackQuery", {"callback_query_id": callback_id})
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": batch_reset_selection_text(0, len(candidates)),
                    "reply_markup": batch_reset_keyboard(bindings, selected_user_ids),
                })
                return
            if action == "batch_cancel":
                finish_batch_reset_session(user_id, chat_id, message_id)
                tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": "已取消"})
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": "请选择要查看的 Key：",
                    "reply_markup": admin_keyboard(bindings),
                })
                return
            if action == "batch_confirm":
                selected_user_ids = finish_batch_reset_session(
                    user_id, chat_id, message_id, valid_user_ids,
                )
                if selected_user_ids is None:
                    tg("answerCallbackQuery", {
                        "callback_query_id": callback_id,
                        "text": "选择已过期，请重新开始。",
                        "show_alert": "true",
                    })
                    return
                if not selected_user_ids:
                    tg("answerCallbackQuery", {
                        "callback_query_id": callback_id,
                        "text": "所选 Key 已不存在，请重新开始。",
                        "show_alert": "true",
                    })
                    return
                selected_count = len(selected_user_ids)
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": f"正在重置 {selected_count} 个 Key…",
                })
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": f"⏳ 正在重置并复查 {selected_count} 个 Key，请稍候…",
                })
                results = reset_selected_keys(bindings, selected_user_ids)
                reset_backup_text = format_reset_backup_snapshots(results)
                if reset_backup_text:
                    notify_admins(config_admins(cfg), reset_backup_text)
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": format_batch_reset_results(results, cfg),
                    "reply_markup": admin_keyboard(bindings),
                })
                return
            operation = {
                "batch_toggle": "toggle",
                "batch_all": "all",
                "batch_clear": "clear",
                "batch_review": "keep",
                "batch_back": "keep",
            }[action]
            selected_user_ids = change_batch_reset_selection(
                user_id,
                chat_id,
                message_id,
                valid_user_ids,
                operation,
                target_user_id if operation == "toggle" else None,
            )
            if selected_user_ids is None:
                tg("answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "选择已过期或 Key 已变更，请重新开始。",
                    "show_alert": "true",
                })
                return
            if action == "batch_review":
                if not selected_user_ids:
                    tg("answerCallbackQuery", {
                        "callback_query_id": callback_id,
                        "text": "请至少选择一个 Key。",
                        "show_alert": "true",
                    })
                    return
                tg("answerCallbackQuery", {"callback_query_id": callback_id})
                tg("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": batch_reset_review_text(bindings, selected_user_ids),
                    "reply_markup": batch_reset_confirmation_keyboard(len(selected_user_ids)),
                })
                return
            tg("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": f"已选择 {len(selected_user_ids)} 个 Key",
            })
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": batch_reset_selection_text(len(selected_user_ids), len(candidates)),
                "reply_markup": batch_reset_keyboard(bindings, selected_user_ids),
            })
            return
        binding = bindings.get(target_user_id)
        if not binding:
            tg("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": "该绑定已不存在，请重新发送 /check。",
                "show_alert": "true",
            })
            return
        key_name = binding["key_name"]
        allowed, retry_after = allow_check(user_id, cooldown=ADMIN_CHECK_COOLDOWN)
        if not allowed:
            tg("answerCallbackQuery", {
                "callback_query_id": callback_id,
                "text": f"查询过于频繁，请 {retry_after} 秒后再试。",
                "show_alert": "true",
            })
            return
        tg("answerCallbackQuery", {"callback_query_id": callback_id})
        data = query_key_usage(key_name, binding["account_id"])
        reply = format_usage(key_name, data or {})
        reply += f"\n\n🔄 刷新时间：{format_refresh_timestamp(cfg)}"
        tg("editMessageText", {
            "chat_id": chat.get("id"),
            "message_id": message.get("message_id"),
            "text": reply,
            "reply_markup": admin_keyboard(bindings, target_user_id),
        })
    except Exception as error:
        log_failure(f"admin {action} user={masked_id(user_id)}", error)
        if action == "batch_confirm":
            error_text = "批量重置执行异常，请重新发送 /check 查询当前用量。"
        elif action.startswith("batch_"):
            error_text = "批量重置操作失败，请重新发送 /check。"
        elif action.startswith("rollback_"):
            error_text = "回滚操作失败；备份仍保留，请重新发送 /check 后重试。"
        elif action.startswith("overview") or action == "ip_detail":
            error_text = "总览查询失败，请稍后再试。"
        else:
            error_text = "查询失败，请稍后再试。"
        tg("sendMessage", {"chat_id": chat.get("id"), "text": error_text})


def handle_update(update):
    message = update.get("message")
    if isinstance(message, dict):
        handle_message(message)
        return
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        handle_callback_query(callback)


class UpdateDispatcher:
    def __init__(self, workers, max_pending):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="telegram-update")
        self.slots = threading.BoundedSemaphore(max_pending)
        self.seen_lock = threading.Lock()
        self.seen_updates = {}

    def submit(self, update_id, update):
        now = time.monotonic()
        with self.seen_lock:
            cutoff = now - 86400
            stale = [key for key, value in self.seen_updates.items() if value < cutoff]
            for key in stale:
                self.seen_updates.pop(key, None)
            if update_id in self.seen_updates:
                return "duplicate"
            if len(self.seen_updates) >= 4096:
                oldest = min(self.seen_updates, key=self.seen_updates.get)
                self.seen_updates.pop(oldest, None)
            if not self.slots.acquire(blocking=False):
                return "busy"
            self.seen_updates[update_id] = now
        try:
            future = self.executor.submit(handle_update, update)
        except Exception:
            with self.seen_lock:
                self.seen_updates.pop(update_id, None)
            self.slots.release()
            raise
        future.add_done_callback(self._completed)
        return "accepted"

    def _completed(self, future):
        self.slots.release()
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            log_failure("webhook update", error)

    def shutdown(self):
        self.executor.shutdown(wait=True, cancel_futures=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "Sub2ApiTgBot/1.2"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.respond(200, b"ok")
        else:
            self.respond(404)

    def log_message(self, fmt, *args):
        return


def dispatch_update_batch(updates, dispatcher, offset=None):
    for update in updates:
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            continue
        update_id = update["update_id"]
        next_offset = max(offset or 0, update_id + 1)
        if not isinstance(update.get("message"), dict) and not isinstance(update.get("callback_query"), dict):
            offset = next_offset
            continue
        result = dispatcher.submit(update_id, update)
        if result == "busy":
            return offset, True
        offset = next_offset
    return offset, False


def poll_updates(dispatcher, stop_event):
    offset = None
    failures = 0
    while not stop_event.is_set():
        params = {
            "timeout": str(POLL_TIMEOUT),
            "limit": "100",
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            params["offset"] = str(offset)
        try:
            updates = tg("getUpdates", params, timeout=POLL_TIMEOUT + 5)
            if not isinstance(updates, list):
                raise RuntimeError("Telegram getUpdates returned a non-list result")
            offset, busy = dispatch_update_batch(updates, dispatcher, offset)
            failures = 0
            if busy:
                stop_event.wait(1)
        except Exception as error:
            log_failure("telegram polling", error)
            failures += 1
            stop_event.wait(min(30, 2 ** min(failures - 1, 5)))


def main():
    validate_runtime_config()
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    tg("deleteWebhook", {"drop_pending_updates": "false"})
    tg("setMyCommands", {"commands": json.dumps([
        {"command": "check", "description": "查询绑定 key 的用量"},
        {"command": "start", "description": "使用说明"},
    ], ensure_ascii=False)})
    print("sub2api tg bot long polling started", flush=True)
    threading.Thread(target=alert_loop, name="weekly-alerts", daemon=True).start()
    dispatcher = UpdateDispatcher(UPDATE_WORKERS, UPDATE_MAX_PENDING)
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    httpd.daemon_threads = True
    health_thread = threading.Thread(target=httpd.serve_forever, name="health-server", daemon=True)
    health_thread.start()
    try:
        poll_updates(dispatcher, stop_event)
    finally:
        httpd.shutdown()
        httpd.server_close()
        health_thread.join(timeout=2)
        dispatcher.shutdown()


if __name__ == "__main__":
    main()
