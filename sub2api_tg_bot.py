#!/usr/bin/env python3
import json
import os
import re
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
BATCH_RESET_SESSION_TTL = 300
OVERVIEW_PAGE_SIZE = 8


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
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def overview_keyboard(page, total_pages):
    rows = []
    if total_pages > 1:
        previous_page = max(0, page - 1)
        next_page = min(total_pages - 1, page + 1)
        rows.append([
            {"text": "◀️", "callback_data": f"overview:{previous_page}"},
            {"text": f"{page + 1}/{total_pages}", "callback_data": f"overview:{page}"},
            {"text": "▶️", "callback_data": f"overview:{next_page}"},
        ])
    rows.extend([
        [{"text": "🔄 刷新总览", "callback_data": f"overview:{page}"}],
        [{"text": "◀️ 返回", "callback_data": "overview_back:0"}],
    ])
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


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


def reset_selected_keys(bindings, selected_user_ids):
    selected_user_ids = set(selected_user_ids)
    results = []
    for target_user_id, binding in reset_candidates(bindings):
        if target_user_id not in selected_user_ids:
            continue
        key_name = binding["key_name"]
        reset_completed = False
        try:
            current_data = query_key_usage(key_name, binding["account_id"]) or {}
            key = current_data.get("key") or {}
            key_id = key.get("id")
            if isinstance(key_id, bool) or not isinstance(key_id, int) or key_id <= 0:
                results.append({
                    "key_name": key_name,
                    "status": "failed",
                    "detail": "未能确定唯一的 Key ID",
                })
                continue
            reset_key_rate_limit_usage(key_id)
            reset_completed = True
            checked_data = query_key_usage(key_name, binding["account_id"]) or {}
            checked_key = checked_data.get("key") or {}
            if not checked_key:
                raise RuntimeError("Post-check did not return API key data")
            results.append({
                "key_name": key_name,
                "status": "success",
                "detail": (
                    f"5h {checked_key.get('usage_5h', '?')} / "
                    f"日 {checked_key.get('usage_1d', '?')} / "
                    f"周 {checked_key.get('usage_7d', '?')}"
                ),
            })
        except Exception as error:
            log_failure(
                f"batch reset target={masked_id(target_user_id)} recheck={str(reset_completed).lower()}",
                error,
            )
            results.append({
                "key_name": key_name,
                "status": "warning" if reset_completed else "failed",
                "detail": "重置成功，但复查失败" if reset_completed else "重置失败",
            })
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


def run_psql_json(sql, variables=None):
    cmd = [PSQL_BIN, "-X", "--set=ON_ERROR_STOP=1", "-tAX"]
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
        "PGOPTIONS": "-c default_transaction_read_only=on -c statement_timeout=10000 -c lock_timeout=2000",
        "PGPASSFILE": "/dev/null",
        "PGPASSWORD": PGPASSWORD,
        "PGPORT": PGPORT,
        "PGSSLMODE": PGSSLMODE,
        "PGUSER": PGUSER,
    }
    out = subprocess.check_output(
        cmd,
        input=sql,
        env=env,
        encoding="utf-8",
        stderr=subprocess.STDOUT,
        timeout=12,
    ).strip()
    if not out:
        return None
    return json.loads(out)


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


def append_account_reset(lines, reset_at, now=None):
    remaining = reset_remaining_text(reset_at, now)
    if remaining:
        timestamp = parse_upstream_timestamp(reset_at).astimezone(ZoneInfo("Asia/Shanghai"))
        lines.append(f"  重置时间：{timestamp.strftime('%Y-%m-%d %H:%M:%S')}｜剩余：{remaining}")


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


def collect_key_overview(bindings):
    overview = []
    for target_user_id, binding in reset_candidates(bindings):
        key_name = binding["key_name"]
        try:
            data = query_key_usage(key_name, binding["account_id"]) or {}
            key = data.get("key") or {}
            if not key:
                raise RuntimeError("Overview query did not return API key data")
            overview.append({
                "key_name": key_name,
                "last_used_at": key.get("last_used_at"),
                "rate_limit_7d": key.get("rate_limit_7d"),
                "usage_7d": key.get("usage_7d"),
            })
        except Exception as error:
            log_failure(f"overview target={masked_id(target_user_id)}", error)
            overview.append({"key_name": key_name, "error": True})
    return overview


def format_key_overview(overview, page=0, page_size=OVERVIEW_PAGE_SIZE):
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
            lines.extend(["• 最后使用：数据不可用", "• 每周额度：数据不可用"])
            continue
        last_used_at = item.get("last_used_at")
        lines.append(
            f"• 最后使用：{format_timestamp(last_used_at) if last_used_at else '暂无使用记录'}"
        )
        append_limit(lines, "每周额度", item.get("rate_limit_7d"), item.get("usage_7d"))
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
    upstream_account = data.get("upstream_account") or {}
    lines = [
        f"🔑 Key：{k.get('name')}",
        f"状态：{format_status(k.get('status'))}",
        "",
        "⏱ 限额",
    ]
    append_limit(lines, "5 小时", k.get("rate_limit_5h"), k.get("usage_5h"))
    if dec(k.get("rate_limit_5h")) > 0:
        append_account_reset(lines, upstream_account.get("reset_5h_at"), now)
    append_limit(lines, "每日", k.get("rate_limit_1d"), k.get("usage_1d"))
    append_limit(lines, "每周", k.get("rate_limit_7d"), k.get("usage_7d"))
    if dec(k.get("rate_limit_7d")) > 0:
        append_account_reset(lines, upstream_account.get("reset_7d_at"), now)
    if upstream_account.get("error") == "not_found":
        lines.append(f"  ⚠️ 未找到配置的上游账号 ID：{upstream_account.get('id')}")
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
        "usage", "overview", "overview_back",
        "batch_start", "batch_toggle", "batch_all", "batch_clear",
        "batch_review", "batch_back", "batch_confirm", "batch_cancel",
        "reset_prompt", "reset_confirm", "reset_cancel",
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
        if action == "overview_back":
            tg("answerCallbackQuery", {"callback_query_id": callback_id})
            tg("editMessageText", {
                "chat_id": chat.get("id"),
                "message_id": message.get("message_id"),
                "text": "请选择要查看的 Key：",
                "reply_markup": admin_keyboard(bindings),
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
            reply, page, total_pages = format_key_overview(overview, int(target_user_id))
            reply += f"\n\n🔄 刷新时间：{format_refresh_timestamp(cfg)}"
            tg("editMessageText", {
                "chat_id": chat.get("id"),
                "message_id": message.get("message_id"),
                "text": reply,
                "reply_markup": overview_keyboard(page, total_pages),
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
        elif action.startswith("overview"):
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
