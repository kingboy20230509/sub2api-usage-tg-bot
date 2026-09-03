#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BOT_SOURCE_FILE="${BOT_SOURCE_FILE:-$SCRIPT_DIR/sub2api_tg_bot.py}"
INSTALL_DIR="${INSTALL_DIR:-/opt/sub2api-tg-bot}"
ENV_FILE="${ENV_FILE:-/etc/sub2api-tg-bot.env}"
SERVICE_FILE="${SERVICE_FILE:-/etc/systemd/system/sub2api-tg-bot.service}"
CONFIG_FILE="${CONFIG_FILE:-$INSTALL_DIR/config.json}"
SERVICE_NAME="${SERVICE_NAME:-sub2api-tg-bot}"
BOT_USER="${BOT_USER:-sub2api-tg-bot}"
BOT_GROUP="${BOT_GROUP:-sub2api-tg-bot}"
LISTEN_HOST="${LISTEN_HOST:-127.0.0.1}"
LISTEN_PORT="${LISTEN_PORT:-8099}"
ALERT_CHECK_INTERVAL="${ALERT_CHECK_INTERVAL:-600}"
ALERT_STATE_PATH="${ALERT_STATE_PATH:-/var/lib/sub2api-tg-bot/alert_state.json}"
AUTO_RESET_CHECK_INTERVAL="${AUTO_RESET_CHECK_INTERVAL:-60}"
AUTO_RESET_STATE_PATH="${AUTO_RESET_STATE_PATH:-/var/lib/sub2api-tg-bot/auto_reset_state.json}"
TIMEZONE="${TIMEZONE:-Asia/Shanghai}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_USER_ID="${TELEGRAM_USER_ID:-}"
SUB2API_KEY_NAME="${SUB2API_KEY_NAME:-}"
PSQL_BIN="${PSQL_BIN:-/usr/bin/psql}"
PGHOST="${PGHOST:-127.0.0.1}"
PGPORT="${PGPORT:-5432}"
PGDATABASE="${PGDATABASE:-sub2api}"
PGUSER="${PGUSER:-sub2api_tg_bot}"
PGPASSWORD="${PGPASSWORD:-}"
PGSSLMODE="${PGSSLMODE:-prefer}"
POLL_TIMEOUT="${POLL_TIMEOUT:-10}"
UPDATE_WORKERS="${UPDATE_WORKERS:-4}"
UPDATE_MAX_PENDING="${UPDATE_MAX_PENDING:-16}"
CHECK_COOLDOWN="${CHECK_COOLDOWN:-10}"
NON_INTERACTIVE="${NON_INTERACTIVE:-0}"

need_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Please run as root: sudo bash install.sh" >&2
    exit 1
  fi
}

have() { command -v "$1" >/dev/null 2>&1; }

validate_safe_path() {
  local name="$1" value="$2"
  if [[ ! "$value" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "$name must be an absolute path containing only A-Z, a-z, 0-9, dot, underscore, dash, and slash" >&2
    exit 1
  fi
}

validate_inputs() {
  validate_safe_path INSTALL_DIR "$INSTALL_DIR"
  validate_safe_path ENV_FILE "$ENV_FILE"
  validate_safe_path SERVICE_FILE "$SERVICE_FILE"
  validate_safe_path CONFIG_FILE "$CONFIG_FILE"
  validate_safe_path ALERT_STATE_PATH "$ALERT_STATE_PATH"
  validate_safe_path AUTO_RESET_STATE_PATH "$AUTO_RESET_STATE_PATH"
  validate_safe_path PSQL_BIN "$PSQL_BIN"
  if [[ ! "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]]; then
    echo "Invalid SERVICE_NAME" >&2
    exit 1
  fi
  if [[ ! "$BOT_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]{0,30}$ ]] || [[ ! "$BOT_GROUP" =~ ^[A-Za-z_][A-Za-z0-9_-]{0,30}$ ]]; then
    echo "Invalid BOT_USER or BOT_GROUP" >&2
    exit 1
  fi
  if [[ ! "$LISTEN_PORT" =~ ^[0-9]{1,5}$ ]] || (( LISTEN_PORT < 1 || LISTEN_PORT > 65535 )); then
    echo "LISTEN_PORT must be between 1 and 65535" >&2
    exit 1
  fi
  if [[ ! "$ALERT_CHECK_INTERVAL" =~ ^[0-9]{1,7}$ ]] || (( ALERT_CHECK_INTERVAL < 60 )); then
    echo "ALERT_CHECK_INTERVAL must be at least 60 seconds" >&2
    exit 1
  fi
  if [[ ! "$AUTO_RESET_CHECK_INTERVAL" =~ ^[0-9]{1,7}$ ]] || (( AUTO_RESET_CHECK_INTERVAL < 60 || AUTO_RESET_CHECK_INTERVAL > 3600 )); then
    echo "AUTO_RESET_CHECK_INTERVAL must be between 60 and 3600 seconds" >&2
    exit 1
  fi
  if [[ ! "$TELEGRAM_USER_ID" =~ ^[0-9]{1,20}$ ]]; then
    echo "TELEGRAM_USER_ID must be a numeric Telegram user ID" >&2
    exit 1
  fi
  if [[ -z "$PGHOST" || "$PGHOST" == *$'\n'* || "$PGHOST" == *$'\r'* ]]; then
    echo "Invalid PGHOST" >&2
    exit 1
  fi
  if [[ ! "$PGPORT" =~ ^[0-9]{1,5}$ ]] || (( PGPORT < 1 || PGPORT > 65535 )); then
    echo "PGPORT must be between 1 and 65535" >&2
    exit 1
  fi
  if [[ ! "$PGDATABASE" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,62}$ ]] || [[ ! "$PGUSER" =~ ^[A-Za-z_][A-Za-z0-9_.-]{0,62}$ ]]; then
    echo "Invalid PGDATABASE or PGUSER" >&2
    exit 1
  fi
  if [[ -z "$PGPASSWORD" || "${PGPASSWORD,,}" == *replace_me* || "$PGPASSWORD" == *$'\n'* || "$PGPASSWORD" == *$'\r'* ]]; then
    echo "PGPASSWORD is required and cannot contain line breaks" >&2
    exit 1
  fi
  if [[ ! "$PGSSLMODE" =~ ^(disable|allow|prefer|require|verify-ca|verify-full)$ ]]; then
    echo "Invalid PGSSLMODE" >&2
    exit 1
  fi
  if [[ "$PGHOST" != "127.0.0.1" && "$PGHOST" != "::1" && "$PGHOST" != "localhost" && "$PGHOST" != /* ]]; then
    if [[ "$PGSSLMODE" != "require" && "$PGSSLMODE" != "verify-ca" && "$PGSSLMODE" != "verify-full" ]]; then
      echo "Remote PostgreSQL connections require PGSSLMODE=require, verify-ca, or verify-full" >&2
      exit 1
    fi
  fi
  if [[ ! "$POLL_TIMEOUT" =~ ^[0-9]{1,2}$ ]] || (( POLL_TIMEOUT < 1 || POLL_TIMEOUT > 50 )); then
    echo "POLL_TIMEOUT must be between 1 and 50 seconds" >&2
    exit 1
  fi
  if [[ ! "$UPDATE_WORKERS" =~ ^[0-9]{1,2}$ ]] || (( UPDATE_WORKERS < 1 || UPDATE_WORKERS > 32 )); then
    echo "UPDATE_WORKERS must be between 1 and 32" >&2
    exit 1
  fi
  if [[ ! "$UPDATE_MAX_PENDING" =~ ^[0-9]{1,3}$ ]] || (( UPDATE_MAX_PENDING < UPDATE_WORKERS || UPDATE_MAX_PENDING > 256 )); then
    echo "UPDATE_MAX_PENDING must be between UPDATE_WORKERS and 256" >&2
    exit 1
  fi
  if [[ ! "$CHECK_COOLDOWN" =~ ^[0-9]{1,4}$ ]] || (( CHECK_COOLDOWN < 1 || CHECK_COOLDOWN > 3600 )); then
    echo "CHECK_COOLDOWN must be between 1 and 3600 seconds" >&2
    exit 1
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

env_quote() {
  python3 -c 'import sys; s=sys.argv[1]; any(c in s for c in "\r\n\0") and sys.exit("environment values cannot contain line breaks or NUL"); print("\"" + s.replace("\\", "\\\\").replace("\"", "\\\"") + "\"")' "$1"
}

main() {
  need_root
  have python3 || { echo "python3 is required" >&2; exit 1; }
  have install || { echo "GNU install is required" >&2; exit 1; }
  have getent || { echo "getent is required" >&2; exit 1; }
  have groupadd || { echo "groupadd is required" >&2; exit 1; }
  have useradd || { echo "useradd is required" >&2; exit 1; }

  read_prompt TELEGRAM_BOT_TOKEN "Telegram Bot Token from @BotFather" "" 1
  read_prompt TELEGRAM_USER_ID "Telegram user ID to bind"
  read_prompt SUB2API_KEY_NAME "Sub2API key name for this Telegram user"
  read_prompt PGPASSWORD "Password for the restricted PostgreSQL role $PGUSER" "" 1

  validate_inputs

  if [[ ! -f "$BOT_SOURCE_FILE" ]]; then
    echo "Missing bot source file: $BOT_SOURCE_FILE" >&2
    echo "Run install.sh from a complete, reviewed repository checkout." >&2
    exit 1
  fi
  if [[ ! -x "$PSQL_BIN" ]]; then
    echo "PostgreSQL client not found or not executable: $PSQL_BIN" >&2
    exit 1
  fi

  if ! getent group "$BOT_GROUP" >/dev/null; then
    groupadd --system "$BOT_GROUP"
  fi
  if ! id -u "$BOT_USER" >/dev/null 2>&1; then
    local nologin_bin
    nologin_bin="$(command -v nologin || true)"
    nologin_bin="${nologin_bin:-/usr/sbin/nologin}"
    useradd --system --gid "$BOT_GROUP" --home-dir /nonexistent --shell "$nologin_bin" "$BOT_USER"
  elif [[ "$(id -gn "$BOT_USER")" != "$BOT_GROUP" ]]; then
    echo "Existing user $BOT_USER does not use expected primary group $BOT_GROUP" >&2
    exit 1
  fi

  install -d -o root -g root -m 0755 "$INSTALL_DIR"
  local alert_state_dir auto_reset_state_dir
  alert_state_dir="$(dirname -- "$ALERT_STATE_PATH")"
  auto_reset_state_dir="$(dirname -- "$AUTO_RESET_STATE_PATH")"
  install -d -o "$BOT_USER" -g "$BOT_GROUP" -m 0700 "$alert_state_dir"
  install -d -o "$BOT_USER" -g "$BOT_GROUP" -m 0700 "$auto_reset_state_dir"
  if [[ -e "$ALERT_STATE_PATH" ]]; then
    chown "$BOT_USER:$BOT_GROUP" "$ALERT_STATE_PATH"
    chmod 0600 "$ALERT_STATE_PATH"
  fi
  if [[ -e "$AUTO_RESET_STATE_PATH" ]]; then
    chown "$BOT_USER:$BOT_GROUP" "$AUTO_RESET_STATE_PATH"
    chmod 0600 "$AUTO_RESET_STATE_PATH"
  fi
  if [[ -f "$CONFIG_FILE" ]]; then
    cp -a "$CONFIG_FILE" "$CONFIG_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  if [[ -f "$ENV_FILE" ]]; then
    cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi
  if [[ -f "$SERVICE_FILE" ]]; then
    cp -a "$SERVICE_FILE" "$SERVICE_FILE.bak.$(date +%Y%m%d-%H%M%S)"
  fi

  install -m 0755 "$BOT_SOURCE_FILE" "$INSTALL_DIR/sub2api_tg_bot.py.new"
  python3 -m py_compile "$INSTALL_DIR/sub2api_tg_bot.py.new"
  mv -f "$INSTALL_DIR/sub2api_tg_bot.py.new" "$INSTALL_DIR/sub2api_tg_bot.py"

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
  chown root:"$BOT_GROUP" "$CONFIG_FILE"
  chmod 0640 "$CONFIG_FILE"

  local token_env listen_host_env config_env state_env auto_reset_state_env
  local psql_bin_env pghost_env pgdatabase_env pguser_env pgpassword_env pgsslmode_env
  token_env="$(env_quote "$TELEGRAM_BOT_TOKEN")"
  listen_host_env="$(env_quote "$LISTEN_HOST")"
  config_env="$(env_quote "$CONFIG_FILE")"
  state_env="$(env_quote "$ALERT_STATE_PATH")"
  auto_reset_state_env="$(env_quote "$AUTO_RESET_STATE_PATH")"
  psql_bin_env="$(env_quote "$PSQL_BIN")"
  pghost_env="$(env_quote "$PGHOST")"
  pgdatabase_env="$(env_quote "$PGDATABASE")"
  pguser_env="$(env_quote "$PGUSER")"
  pgpassword_env="$(env_quote "$PGPASSWORD")"
  pgsslmode_env="$(env_quote "$PGSSLMODE")"
  cat > "$ENV_FILE" <<ENV
TELEGRAM_BOT_TOKEN=$token_env
LISTEN_HOST=$listen_host_env
LISTEN_PORT=$LISTEN_PORT
SUB2API_TG_BOT_CONFIG=$config_env
ALERT_CHECK_INTERVAL=$ALERT_CHECK_INTERVAL
ALERT_STATE_PATH=$state_env
AUTO_RESET_CHECK_INTERVAL=$AUTO_RESET_CHECK_INTERVAL
AUTO_RESET_STATE_PATH=$auto_reset_state_env
PSQL_BIN=$psql_bin_env
PGHOST=$pghost_env
PGPORT=$PGPORT
PGDATABASE=$pgdatabase_env
PGUSER=$pguser_env
PGPASSWORD=$pgpassword_env
PGSSLMODE=$pgsslmode_env
POLL_TIMEOUT=$POLL_TIMEOUT
UPDATE_WORKERS=$UPDATE_WORKERS
UPDATE_MAX_PENDING=$UPDATE_MAX_PENDING
CHECK_COOLDOWN=$CHECK_COOLDOWN
ENV
  chmod 0600 "$ENV_FILE"

  cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=Telegram bot for Sub2API key usage checks
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/python3 $INSTALL_DIR/sub2api_tg_bot.py
Restart=always
RestartSec=2
User=$BOT_USER
Group=$BOT_GROUP
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
PrivateDevices=true
PrivateTmp=true
ProtectClock=true
ProtectControlGroups=true
ProtectHome=true
ProtectHostname=true
ProtectKernelLogs=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectSystem=strict
ReadWritePaths=$alert_state_dir
LockPersonality=true
MemoryDenyWriteExecute=true
ProcSubset=pid
ProtectProc=invisible
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallFilter=@system-service
SystemCallArchitectures=native

[Install]
WantedBy=multi-user.target
SERVICE

  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME.service"

  echo
  echo "Installed $SERVICE_NAME"
  echo "Config: $CONFIG_FILE"
  echo "Env:    $ENV_FILE"
  echo "Mode:   Telegram long polling (no public domain or reverse proxy required)"
  echo "Health: curl http://$LISTEN_HOST:$LISTEN_PORT/health"
  echo
  echo "Check logs: journalctl -u $SERVICE_NAME.service -f"
}

main "$@"
