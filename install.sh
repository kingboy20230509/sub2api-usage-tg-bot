#!/usr/bin/env bash
set -Eeuo pipefail

REPO_RAW_BASE="${REPO_RAW_BASE:-https://raw.githubusercontent.com/chainfix/sub2api-usage-tg-bot/main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/sub2api-tg-bot}"
ENV_FILE="${ENV_FILE:-/etc/sub2api-tg-bot.env}"
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/sub2api-tg-bot.service}"
CONFIG_FILE="${CONFIG_FILE:-$INSTALL_DIR/config.json}"
SERVICE_NAME="${SERVICE_NAME:-sub2api-tg-bot}"
LISTEN_HOST="${LISTEN_HOST:-127.0.0.1}"
LISTEN_PORT="${LISTEN_PORT:-8099}"
TIMEZONE="${TIMEZONE:-Asia/Shanghai}"
WEBHOOK_PATH="${WEBHOOK_PATH:-}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"
PUBLIC_WEBHOOK_URL="${PUBLIC_WEBHOOK_URL:-}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_USER_ID="${TELEGRAM_USER_ID:-}"
SUB2API_KEY_NAME="${SUB2API_KEY_NAME:-}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"
SKIP_NGINX_HINT="${SKIP_NGINX_HINT:-0}"

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run as root: sudo bash install.sh" >&2
    exit 1
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

random_secret() {
  if have openssl; then
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' | cut -c1-48
  else
    tr -dc 'A-Za-z0-9_-'< /dev/urandom | head -c 48
  fi
}

read_prompt() {
  local var_name="$1" prompt="$2" default_value="${3:-}" secret="${4:-0}" value
  if [[ -n "${!var_name:-}" ]]; then
    return 0
  fi
  if [[ "$NON_INTERACTIVE" == "1" ]]; then
    if [[ -n "$default_value" ]]; then
      printf -v "$var_name" '%s' "$default_value"
      return 0
    fi
    echo "Missing required variable: $var_name" >&2
    exit 1
  fi
  local input_fd="/dev/stdin"
  if [[ -r /dev/tty ]]; then
    input_fd="/dev/tty"
  fi
  if [[ "$secret" == "1" ]]; then
    read -r -s -p "$prompt" value < "$input_fd"
    echo
  else
    if [[ -n "$default_value" ]]; then
      read -r -p "$prompt [$default_value]: " value < "$input_fd"
      value="${value:-$default_value}"
    else
      read -r -p "$prompt: " value < "$input_fd"
    fi
  fi
  printf -v "$var_name" '%s' "$value"
}

json_escape() {
  python3 -c 'import json,sys; print(json.dumps(sys.argv[1], ensure_ascii=False))' "$1"
}

fetch_file() {
  local url="$1" out="$2"
  if have curl; then
    curl -fsSL "$url" -o "$out"
  elif have wget; then
    wget -qO "$out" "$url"
  else
    echo "curl or wget is required" >&2
    exit 1
  fi
}

main() {
  need_root
  have python3 || { echo "python3 is required" >&2; exit 1; }
  have docker || echo "Warning: docker command not found. The bot needs Docker access to query sub2api-postgres." >&2

  read_prompt TELEGRAM_BOT_TOKEN "Telegram Bot Token from @BotFather" "" 1
  read_prompt TELEGRAM_USER_ID "Telegram user ID to bind"
  read_prompt SUB2API_KEY_NAME "Sub2API key name for this Telegram user"

  if [[ -z "$WEBHOOK_SECRET" ]]; then
    WEBHOOK_SECRET="$(random_secret)"
  fi
  if [[ -z "$WEBHOOK_PATH" ]]; then
    WEBHOOK_PATH="/tg-sub2api-bot/$(random_secret)"
  fi
  read_prompt PUBLIC_WEBHOOK_URL "Public webhook URL, e.g. https://example.com${WEBHOOK_PATH}" "${PUBLIC_WEBHOOK_URL:-}"

  mkdir -p "$INSTALL_DIR"
  if [[ -f "$CONFIG_FILE" ]]; then
    cp -a "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  if [[ -f "$ENV_FILE" ]]; then
    cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  if [[ -f "$SERVICE_FILE" ]]; then
    cp -a "$SERVICE_FILE" "$SERVICE_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi

  fetch_file "$REPO_RAW_BASE/sub2api_tg_bot.py" "$INSTALL_DIR/sub2api_tg_bot.py"
  chmod 0755 "$INSTALL_DIR/sub2api_tg_bot.py"

  local uid_json key_json tz_json
  uid_json="$(json_escape "$TELEGRAM_USER_ID")"
  key_json="$(json_escape "$SUB2API_KEY_NAME")"
  tz_json="$(json_escape "$TIMEZONE")"
  cat > "$CONFIG_FILE" <<JSON
{
  "bindings": {
    $uid_json: $key_json
  },
  "timezone": $tz_json
}
JSON
  chmod 0600 "$CONFIG_FILE"

  cat > "$ENV_FILE" <<ENV
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
WEBHOOK_SECRET=$WEBHOOK_SECRET
PUBLIC_WEBHOOK_URL=$PUBLIC_WEBHOOK_URL
WEBHOOK_PATH=$WEBHOOK_PATH
LISTEN_HOST=$LISTEN_HOST
LISTEN_PORT=$LISTEN_PORT
SUB2API_TG_BOT_CONFIG=$CONFIG_FILE
ENV
  chmod 0600 "$ENV_FILE"

  cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Telegram bot for Sub2API key usage checks
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $INSTALL_DIR/sub2api_tg_bot.py
Restart=always
RestartSec=2
User=root
Group=root

[Install]
WantedBy=multi-user.target
SERVICE

  python3 -m py_compile "$INSTALL_DIR/sub2api_tg_bot.py"
  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME.service"

  echo
  echo "Installed $SERVICE_NAME"
  echo "Config: $CONFIG_FILE"
  echo "Env:    $ENV_FILE"
  echo "Listen: http://$LISTEN_HOST:$LISTEN_PORT"
  echo "Health: curl http://$LISTEN_HOST:$LISTEN_PORT/health"
  echo
  if [[ "$SKIP_NGINX_HINT" != "1" ]]; then
    echo "Reverse proxy example:"
    echo "location $WEBHOOK_PATH {"
    echo "    proxy_pass http://$LISTEN_HOST:$LISTEN_PORT$WEBHOOK_PATH;"
    echo '    proxy_set_header Host $host;'
    echo '    proxy_set_header X-Real-IP $remote_addr;'
    echo '    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;'
    echo '    proxy_set_header X-Forwarded-Proto $scheme;'
    echo "}"
    echo
  fi
  echo "Check logs: journalctl -u $SERVICE_NAME.service -f"
}

main "$@"
