#!/usr/bin/env python3
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("SUB2API_TG_BOT_CONFIG", os.path.join(BASE_DIR, "config.json"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/tg-sub2api-bot").strip()
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8099"))
PUBLIC_WEBHOOK_URL = os.environ.get("PUBLIC_WEBHOOK_URL", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
KEY_NAME_RE = re.compile(r"^[\w .:@+-]{1,100}$")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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


def sql_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_psql_json(sql):
    cmd = ["docker", "exec", "sub2api-postgres", "psql", "-U", "sub2api", "-d", "sub2api", "-tAX", "-c", sql]
    out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=10).strip()
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
    s = f"{d:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"


def num(v):
    return str(int(v or 0))


def query_key_usage(key_name):
    if not KEY_NAME_RE.match(key_name):
        raise ValueError("Invalid key name in binding config")
    q = sql_quote(key_name)
    sql = f"""
WITH k AS (
  SELECT id, name, status, quota, quota_used, usage_5h, usage_1d, usage_7d,
         last_used_at, created_at, expires_at
  FROM api_keys
  WHERE name = {q} AND deleted_at IS NULL
  ORDER BY id ASC
  LIMIT 1
), agg_all AS (
  SELECT count(*)::bigint requests,
         coalesce(sum(input_tokens),0)::bigint input_tokens,
         coalesce(sum(output_tokens),0)::bigint output_tokens,
         coalesce(sum(cache_creation_tokens),0)::bigint cache_creation_tokens,
         coalesce(sum(cache_read_tokens),0)::bigint cache_read_tokens,
         coalesce(sum(actual_cost),0)::numeric(20,10) actual_cost
  FROM usage_logs WHERE api_key_id = (SELECT id FROM k)
), agg_today AS (
  SELECT count(*)::bigint requests,
         coalesce(sum(input_tokens),0)::bigint input_tokens,
         coalesce(sum(output_tokens),0)::bigint output_tokens,
         coalesce(sum(actual_cost),0)::numeric(20,10) actual_cost
  FROM usage_logs
  WHERE api_key_id = (SELECT id FROM k)
    AND created_at >= date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'
), models AS (
  SELECT coalesce(nullif(requested_model,''), model) model,
         count(*)::bigint requests,
         coalesce(sum(actual_cost),0)::numeric(20,10) actual_cost
  FROM usage_logs
  WHERE api_key_id = (SELECT id FROM k)
  GROUP BY 1
  ORDER BY requests DESC, actual_cost DESC
  LIMIT 5
)
SELECT json_build_object(
  'key', (SELECT row_to_json(k) FROM k),
  'all', (SELECT row_to_json(agg_all) FROM agg_all),
  'today', (SELECT row_to_json(agg_today) FROM agg_today),
  'models', coalesce((SELECT json_agg(models) FROM models), '[]'::json)
)::text;
"""
    return run_psql_json(sql)


def format_usage(key_name, data):
    k = data.get("key")
    if not k:
        return f"未找到绑定的 key：{key_name}"
    all_ = data.get("all") or {}
    today = data.get("today") or {}
    models = data.get("models") or []
    quota = dec(k.get("quota"))
    used = dec(k.get("quota_used"))
    quota_line = f"额度：{money(used)} / {money(quota)}，剩余 {money(quota - used)}" if quota > 0 else f"累计扣费：{money(all_.get('actual_cost'))}"
    lines = [
        f"🔑 Key：{k.get('name')}（状态：{k.get('status')}）",
        quota_line,
        f"最近 5h / 1d / 7d：{money(k.get('usage_5h'))} / {money(k.get('usage_1d'))} / {money(k.get('usage_7d'))}",
        "",
        "今日用量：",
        f"• 请求：{num(today.get('requests'))}",
        f"• Tokens：输入 {num(today.get('input_tokens'))} / 输出 {num(today.get('output_tokens'))}",
        f"• 费用：{money(today.get('actual_cost'))}",
        "",
        "累计用量：",
        f"• 请求：{num(all_.get('requests'))}",
        f"• Tokens：输入 {num(all_.get('input_tokens'))} / 输出 {num(all_.get('output_tokens'))}",
        f"• Cache：写入 {num(all_.get('cache_creation_tokens'))} / 读取 {num(all_.get('cache_read_tokens'))}",
        f"• 费用：{money(all_.get('actual_cost'))}",
    ]
    if k.get("last_used_at"):
        lines.append(f"• 最近使用：{k.get('last_used_at')}")
    if models:
        lines += ["", "模型 Top："]
        for m in models:
            lines.append(f"• {m.get('model') or '-'}：{num(m.get('requests'))} 次 / {money(m.get('actual_cost'))}")
    return "\n".join(lines)


def handle_message(msg):
    chat = msg.get("chat", {})
    user = msg.get("from", {})
    text_in = (msg.get("text") or "").strip()
    chat_id = chat.get("id")
    user_id = str(user.get("id"))
    if not chat_id or not text_in:
        return
    print(f"received chat={chat_id} user={user_id} text={text_in!r}", flush=True)
    cmd = text_in.split()[0].split("@", 1)[0].lower()
    if cmd == "/start":
        tg("sendMessage", {"chat_id": chat_id, "text": "发送 /check 查询你绑定的 Sub2API key 用量。"})
        return
    if cmd != "/check":
        return
    cfg = load_config()
    key_name = (cfg.get("bindings") or {}).get(user_id)
    if not key_name:
        tg("sendMessage", {"chat_id": chat_id, "text": "你的 Telegram ID 还没有绑定 key。"})
        return
    t0 = time.perf_counter()
    try:
        data = query_key_usage(key_name)
        t1 = time.perf_counter()
        reply = format_usage(key_name, data or {})
        t2 = time.perf_counter()
        tg("sendMessage", {"chat_id": chat_id, "text": reply})
        t3 = time.perf_counter()
        print(f"check timing user={user_id} key={key_name} query={t1-t0:.3f}s format={t2-t1:.3f}s send={t3-t2:.3f}s total={t3-t0:.3f}s", flush=True)
    except Exception as e:
        print(f"check failed user={user_id}: {e}", file=sys.stderr, flush=True)
        tg("sendMessage", {"chat_id": chat_id, "text": "查询失败，请稍后再试。"})


class Handler(BaseHTTPRequestHandler):
    server_version = "Sub2ApiTgBot/1.0"

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != WEBHOOK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        if WEBHOOK_SECRET and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(min(length, 1048576))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
        try:
            upd = json.loads(body.decode("utf-8"))
            if "message" in upd:
                handle_message(upd["message"])
        except Exception as e:
            print(f"webhook update failed: {e}", file=sys.stderr, flush=True)

    def log_message(self, fmt, *args):
        return


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    tg("deleteWebhook", {"drop_pending_updates": "false"})
    tg("setMyCommands", {"commands": json.dumps([
        {"command": "check", "description": "查询绑定 key 的用量"},
        {"command": "start", "description": "使用说明"},
    ], ensure_ascii=False)})
    if PUBLIC_WEBHOOK_URL:
        tg("setWebhook", {"url": PUBLIC_WEBHOOK_URL, "secret_token": WEBHOOK_SECRET, "allowed_updates": json.dumps(["message"])})
    print(f"sub2api tg bot webhook started on {LISTEN_HOST}:{LISTEN_PORT}{WEBHOOK_PATH}", flush=True)
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
