\set ON_ERROR_STOP on

-- Run this file as the Sub2API database owner. It creates a login that can
-- execute one fixed aggregation function, but cannot read Sub2API tables.

SELECT 'CREATE ROLE sub2api_tg_bot LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 5'
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'sub2api_tg_bot')
\gexec

ALTER ROLE sub2api_tg_bot WITH
  LOGIN
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  NOREPLICATION
  NOBYPASSRLS
  CONNECTION LIMIT 5;
ALTER ROLE sub2api_tg_bot SET default_transaction_read_only = on;
ALTER ROLE sub2api_tg_bot SET statement_timeout = '10s';
ALTER ROLE sub2api_tg_bot SET lock_timeout = '2s';
ALTER ROLE sub2api_tg_bot SET temp_file_limit = '16MB';

CREATE SCHEMA IF NOT EXISTS sub2api_tg_bot_api;
REVOKE ALL ON SCHEMA sub2api_tg_bot_api FROM PUBLIC;

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.usage(p_key_name text)
RETURNS json
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH bounds AS (
  SELECT
    (date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') - interval '6 days')
      AT TIME ZONE 'Asia/Shanghai' AS seven_days_start,
    date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai')
      AT TIME ZONE 'Asia/Shanghai' AS today_start
), matching_keys AS (
  SELECT id, name, status, quota, quota_used,
         rate_limit_5h, rate_limit_1d, rate_limit_7d,
         usage_5h, usage_1d, usage_7d,
         window_5h_start, window_1d_start, window_7d_start,
         last_used_at, created_at, expires_at
  FROM public.api_keys
  WHERE name = p_key_name AND deleted_at IS NULL
), match_count AS (
  SELECT count(*)::integer AS total
  FROM matching_keys
), k AS (
  SELECT id, name, status, quota, quota_used,
         rate_limit_5h, rate_limit_1d, rate_limit_7d,
         usage_5h, usage_1d, usage_7d,
         window_5h_start, window_1d_start, window_7d_start,
         CASE WHEN window_7d_start IS NOT NULL
              THEN window_7d_start + interval '7 days'
         END AS window_7d_end,
         last_used_at, created_at, expires_at
  FROM matching_keys
  WHERE (SELECT total FROM match_count) = 1
), agg_7d AS (
  SELECT (SELECT seven_days_start FROM bounds) AS window_start,
         (SELECT today_start FROM bounds) AS window_end,
         count(*)::bigint requests,
         coalesce(sum(input_tokens), 0)::bigint input_tokens,
         coalesce(sum(output_tokens), 0)::bigint output_tokens,
         coalesce(sum(cache_creation_tokens), 0)::bigint cache_creation_tokens,
         coalesce(sum(cache_read_tokens), 0)::bigint cache_read_tokens,
         coalesce(sum(actual_cost), 0)::numeric(20, 10) actual_cost
  FROM public.usage_logs
  WHERE api_key_id = (SELECT id FROM k)
    AND created_at >= (SELECT seven_days_start FROM bounds)
    AND created_at < (SELECT today_start FROM bounds) + interval '1 day'
), agg_today AS (
  SELECT count(*)::bigint requests,
         coalesce(sum(input_tokens), 0)::bigint input_tokens,
         coalesce(sum(output_tokens), 0)::bigint output_tokens,
         coalesce(sum(cache_creation_tokens), 0)::bigint cache_creation_tokens,
         coalesce(sum(cache_read_tokens), 0)::bigint cache_read_tokens,
         coalesce(sum(actual_cost), 0)::numeric(20, 10) actual_cost
  FROM public.usage_logs
  WHERE api_key_id = (SELECT id FROM k)
    AND created_at >= (SELECT today_start FROM bounds)
    AND created_at < (SELECT today_start FROM bounds) + interval '1 day'
), models_7d AS (
  SELECT coalesce(nullif(requested_model, ''), nullif(model, ''), 'unknown') model,
         count(*)::bigint requests,
         coalesce(sum(input_tokens), 0)::bigint input_tokens,
         coalesce(sum(output_tokens), 0)::bigint output_tokens,
         coalesce(sum(cache_creation_tokens), 0)::bigint cache_creation_tokens,
         coalesce(sum(cache_read_tokens), 0)::bigint cache_read_tokens,
         coalesce(sum(actual_cost), 0)::numeric(20, 10) actual_cost
  FROM public.usage_logs
  WHERE api_key_id = (SELECT id FROM k)
    AND created_at >= (SELECT seven_days_start FROM bounds)
    AND created_at < (SELECT today_start FROM bounds) + interval '1 day'
  GROUP BY 1
  ORDER BY requests DESC, actual_cost DESC
  LIMIT 5
), models_today AS (
  SELECT coalesce(nullif(requested_model, ''), nullif(model, ''), 'unknown') model,
         count(*)::bigint requests,
         coalesce(sum(input_tokens), 0)::bigint input_tokens,
         coalesce(sum(output_tokens), 0)::bigint output_tokens,
         coalesce(sum(cache_creation_tokens), 0)::bigint cache_creation_tokens,
         coalesce(sum(cache_read_tokens), 0)::bigint cache_read_tokens,
         coalesce(sum(actual_cost), 0)::numeric(20, 10) actual_cost
  FROM public.usage_logs
  WHERE api_key_id = (SELECT id FROM k)
    AND created_at >= (SELECT today_start FROM bounds)
    AND created_at < (SELECT today_start FROM bounds) + interval '1 day'
  GROUP BY 1
  ORDER BY requests DESC, actual_cost DESC
  LIMIT 5
)
SELECT CASE
  WHEN (SELECT total FROM match_count) = 0
    THEN json_build_object('error', 'not_found')
  WHEN (SELECT total FROM match_count) > 1
    THEN json_build_object('error', 'duplicate_key_name')
  ELSE json_build_object(
    'key', (SELECT row_to_json(k) FROM k),
    'seven_days', (SELECT row_to_json(agg_7d) FROM agg_7d),
    'today', (SELECT row_to_json(agg_today) FROM agg_today),
    'models_7d', coalesce((SELECT json_agg(models_7d) FROM models_7d), '[]'::json),
    'models_today', coalesce((SELECT json_agg(models_today) FROM models_today), '[]'::json)
  )
END;
$function$;

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.usage_with_account(
  p_key_name text,
  p_account_id bigint
)
RETURNS json
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH base AS (
  SELECT sub2api_tg_bot_api.usage(p_key_name)::jsonb AS payload
), account_snapshot AS (
  SELECT jsonb_build_object(
    'id', id,
    'platform', platform,
    'type', type,
    'snapshot_updated_at', extra->>'codex_usage_updated_at',
    'reset_5h_at', extra->>'codex_5h_reset_at',
    'reset_7d_at', extra->>'codex_7d_reset_at'
  ) AS payload
  FROM public.accounts
  WHERE id = p_account_id
    AND deleted_at IS NULL
)
SELECT (
  base.payload || jsonb_build_object(
    'upstream_account',
    coalesce(
      (SELECT payload FROM account_snapshot),
      jsonb_build_object('error', 'not_found', 'id', p_account_id)
    )
  )
)::json
FROM base;
$function$;

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.account_estimate(
  p_account_id bigint
)
RETURNS json
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH account_snapshot AS (
  SELECT
    id,
    name,
    platform,
    type,
    extra->>'codex_usage_updated_at' AS snapshot_updated_at,
    extra->>'codex_7d_used_percent' AS used_7d_percent,
    CASE
      WHEN extra->>'codex_7d_reset_at' ~
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})$'
      THEN (extra->>'codex_7d_reset_at')::timestamptz
    END AS reset_7d_at,
    CASE
      WHEN extra->>'codex_7d_window_minutes' ~ '^[0-9]+$'
        AND (extra->>'codex_7d_window_minutes')::bigint > 0
      THEN (extra->>'codex_7d_window_minutes')::bigint
      ELSE 10080
    END AS window_7d_minutes
  FROM public.accounts
  WHERE id = p_account_id
    AND deleted_at IS NULL
), account_cost AS (
  SELECT coalesce(sum(
    coalesce(
      nullif(to_jsonb(usage_row)->>'account_stats_cost', '')::numeric,
      usage_row.total_cost
    ) * coalesce(
      nullif(to_jsonb(usage_row)->>'account_rate_multiplier', '')::numeric,
      1
    )
  ), 0)::numeric(20, 10) AS consumed_amount
  FROM public.usage_logs AS usage_row
  JOIN account_snapshot AS account ON account.id = usage_row.account_id
  WHERE account.reset_7d_at IS NOT NULL
    AND usage_row.created_at >= account.reset_7d_at - account.window_7d_minutes * interval '1 minute'
    AND usage_row.created_at < account.reset_7d_at
)
SELECT CASE
  WHEN NOT EXISTS (SELECT 1 FROM account_snapshot)
    THEN json_build_object('error', 'not_found', 'id', p_account_id)
  ELSE json_build_object(
    'id', (SELECT id FROM account_snapshot),
    'name', (SELECT name FROM account_snapshot),
    'platform', (SELECT platform FROM account_snapshot),
    'type', (SELECT type FROM account_snapshot),
    'snapshot_updated_at', (SELECT snapshot_updated_at FROM account_snapshot),
    'used_7d_percent', (SELECT used_7d_percent FROM account_snapshot),
    'window_start', (
      SELECT reset_7d_at - window_7d_minutes * interval '1 minute'
      FROM account_snapshot
    ),
    'window_end', (SELECT reset_7d_at FROM account_snapshot),
    'consumed_amount', CASE
      WHEN (SELECT reset_7d_at FROM account_snapshot) IS NULL THEN NULL
      ELSE (SELECT consumed_amount FROM account_cost)
    END
  )
END;
$function$;

REVOKE ALL PRIVILEGES ON DATABASE sub2api FROM sub2api_tg_bot;
REVOKE ALL PRIVILEGES ON TABLE public.api_keys, public.usage_logs, public.accounts FROM sub2api_tg_bot;
REVOKE CREATE ON SCHEMA public FROM sub2api_tg_bot;
REVOKE ALL ON SCHEMA sub2api_tg_bot_api FROM sub2api_tg_bot;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.usage(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.usage_with_account(text, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.account_estimate(bigint) FROM PUBLIC;

GRANT CONNECT ON DATABASE sub2api TO sub2api_tg_bot;
GRANT USAGE ON SCHEMA sub2api_tg_bot_api TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.usage(text) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.usage_with_account(text, bigint) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.account_estimate(bigint) TO sub2api_tg_bot;
