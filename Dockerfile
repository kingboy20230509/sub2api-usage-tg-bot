FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUB2API_TG_BOT_CONFIG=/etc/sub2api-tg-bot/config.json \
    ALERT_STATE_PATH=/var/lib/sub2api-tg-bot/alert_state.json \
    PSQL_BIN=/usr/bin/psql \
    LISTEN_HOST=127.0.0.1 \
    LISTEN_PORT=8099

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 sub2api-tg-bot \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin sub2api-tg-bot \
    && install -d -o 10001 -g 10001 -m 0700 /var/lib/sub2api-tg-bot

WORKDIR /app
COPY --chown=10001:10001 sub2api_tg_bot.py docker-healthcheck.py ./

USER 10001:10001
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python3", "/app/docker-healthcheck.py"]

ENTRYPOINT ["python3", "/app/sub2api_tg_bot.py"]
