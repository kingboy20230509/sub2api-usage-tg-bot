/opt/homebrew/Library/Homebrew/cmd/shellenv.sh: line 27: /bin/ps: Operation not permitted
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
), k AS (
  SELECT id, name, status, quota, quota_used,
         rate_limit_5h, rate_limit_1d, rate_limit_7d,
         usage_5h, usage_1d, usage_7d,
         window_5h_start, window_1d_start, window_7d_start,
         CASE WHEN window_7d_start IS NOT NULL
              THEN window_7d_start + interval '7 days'
         END AS window_7d_end,
         last_used_at, created_at, expires_at
  FROM public.api_keys
  WHERE name = p_key_name AND deleted_at IS NULL
  ORDER BY id ASC
  LIMIT 1
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
SELECT json_build_object(
  'key', (SELECT row_to_json(k) FROM k),
  'seven_days', (SELECT row_to_json(agg_7d) FROM agg_7d),
  'today', (SELECT row_to_json(agg_today) FROM agg_today),
  'models_7d', coalesce((SELECT json_agg(models_7d) FROM models_7d), '[]'::json),
  'models_today', coalesce((SELECT json_agg(models_today) FROM models_today), '[]'::json)
);
$function$;

REVOKE ALL PRIVILEGES ON DATABASE sub2api FROM sub2api_tg_bot;
REVOKE ALL PRIVILEGES ON TABLE public.api_keys, public.usage_logs FROM sub2api_tg_bot;
REVOKE CREATE ON SCHEMA public FROM sub2api_tg_bot;
REVOKE ALL ON SCHEMA sub2api_tg_bot_api FROM sub2api_tg_bot;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.usage(text) FROM PUBLIC;

GRANT CONNECT ON DATABASE sub2api TO sub2api_tg_bot;
GRANT USAGE ON SCHEMA sub2api_tg_bot_api TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.usage(text) TO sub2api_tg_bot;
