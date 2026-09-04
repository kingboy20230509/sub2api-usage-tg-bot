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

CREATE TABLE IF NOT EXISTS sub2api_tg_bot_api.rate_limit_backups (
  backup_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  api_key_id bigint NOT NULL,
  key_name text NOT NULL,
  account_id bigint,
  reset_source text NOT NULL CHECK (reset_source IN ('manual', 'auto')),
  batch_id text,
  snapshot jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE sub2api_tg_bot_api.rate_limit_backups
  ADD COLUMN IF NOT EXISTS batch_id text;
CREATE INDEX IF NOT EXISTS rate_limit_backups_batch_id_idx
  ON sub2api_tg_bot_api.rate_limit_backups(batch_id)
  WHERE batch_id IS NOT NULL;
REVOKE ALL ON TABLE sub2api_tg_bot_api.rate_limit_backups FROM PUBLIC;
REVOKE ALL ON TABLE sub2api_tg_bot_api.rate_limit_backups FROM sub2api_tg_bot;
REVOKE ALL ON SEQUENCE sub2api_tg_bot_api.rate_limit_backups_backup_id_seq FROM PUBLIC;
REVOKE ALL ON SEQUENCE sub2api_tg_bot_api.rate_limit_backups_backup_id_seq FROM sub2api_tg_bot;

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

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.key_overview(p_key_name text)
RETURNS json
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH bounds AS (
  SELECT date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai')
    AT TIME ZONE 'Asia/Shanghai' AS today_start
), matching_keys AS (
  SELECT id, name, last_used_at, rate_limit_7d, usage_7d
  FROM public.api_keys
  WHERE name = p_key_name AND deleted_at IS NULL
), match_count AS (
  SELECT count(*)::integer AS total FROM matching_keys
), key_row AS (
  SELECT * FROM matching_keys WHERE (SELECT total FROM match_count) = 1
), ip_counts AS (
  SELECT
    count(DISTINCT ip_address) FILTER (
      WHERE created_at >= (SELECT today_start FROM bounds)
        AND created_at < (SELECT today_start FROM bounds) + interval '1 day'
    )::bigint AS today,
    count(DISTINCT ip_address) FILTER (
      WHERE created_at >= (SELECT today_start FROM bounds) - interval '1 day'
        AND created_at < (SELECT today_start FROM bounds)
    )::bigint AS yesterday
  FROM public.usage_logs
  WHERE api_key_id = (SELECT id FROM key_row)
    AND ip_address IS NOT NULL
    AND btrim(ip_address) <> ''
    AND created_at >= (SELECT today_start FROM bounds) - interval '1 day'
    AND created_at < (SELECT today_start FROM bounds) + interval '1 day'
)
SELECT CASE
  WHEN (SELECT total FROM match_count) = 0
    THEN json_build_object('error', 'not_found')
  WHEN (SELECT total FROM match_count) > 1
    THEN json_build_object('error', 'duplicate_key_name')
  ELSE json_build_object(
    'key', (SELECT row_to_json(key_row) FROM key_row),
    'ip_counts', (SELECT row_to_json(ip_counts) FROM ip_counts)
  )
END;
$function$;

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.key_ip_history(
  p_key_name text,
  p_offset integer,
  p_limit integer
)
RETURNS json
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH bounds AS (
  SELECT
    (date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') - interval '2 days')
      AT TIME ZONE 'Asia/Shanghai' AS window_start,
    now() AS window_end
), matching_keys AS (
  SELECT id
  FROM public.api_keys
  WHERE name = p_key_name AND deleted_at IS NULL
), match_count AS (
  SELECT count(*)::integer AS total FROM matching_keys
), ip_rows AS (
  SELECT
    ip_address,
    min(created_at) AS first_seen,
    max(created_at) AS last_seen,
    count(*)::bigint AS requests
  FROM public.usage_logs
  WHERE api_key_id = (SELECT id FROM matching_keys)
    AND ip_address IS NOT NULL
    AND btrim(ip_address) <> ''
    AND created_at >= (SELECT window_start FROM bounds)
    AND created_at <= (SELECT window_end FROM bounds)
    AND (SELECT total FROM match_count) = 1
  GROUP BY ip_address
), paged_ips AS (
  SELECT ip_address, first_seen, last_seen, requests
  FROM ip_rows
  ORDER BY last_seen DESC, ip_address
  OFFSET coalesce(greatest(p_offset, 0), 0)
  LIMIT coalesce(greatest(p_limit, 0), 0)
)
SELECT CASE
  WHEN p_offset IS NULL OR p_limit IS NULL
    OR p_offset < 0 OR p_offset > 10000 OR p_limit < 1 OR p_limit > 20
    THEN json_build_object('error', 'invalid_pagination')
  WHEN (SELECT total FROM match_count) = 0
    THEN json_build_object('error', 'not_found')
  WHEN (SELECT total FROM match_count) > 1
    THEN json_build_object('error', 'duplicate_key_name')
  ELSE json_build_object(
    'window_start', (SELECT window_start FROM bounds),
    'window_end', (SELECT window_end FROM bounds),
    'total', (SELECT count(*)::bigint FROM ip_rows),
    'ips', coalesce((
      SELECT json_agg(row_to_json(paged_ips) ORDER BY last_seen DESC, ip_address)
      FROM paged_ips
    ), '[]'::json)
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

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.account_weekly_reset(
  p_account_id bigint
)
RETURNS json
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
SELECT CASE
  WHEN account.id IS NULL
    THEN json_build_object('error', 'not_found', 'id', p_account_id)
  ELSE json_build_object(
    'id', account.id,
    'snapshot_updated_at', account.extra->>'codex_usage_updated_at',
    'reset_7d_at', account.extra->>'codex_7d_reset_at'
  )
END
FROM (SELECT 1) AS seed
LEFT JOIN public.accounts AS account
  ON account.id = p_account_id
 AND account.deleted_at IS NULL;
$function$;

DROP FUNCTION IF EXISTS sub2api_tg_bot_api.backup_rate_limits(text, bigint, text);

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.backup_rate_limits(
  p_key_name text,
  p_account_id bigint,
  p_reset_source text,
  p_batch_id text
)
RETURNS json
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_key public.api_keys%ROWTYPE;
  v_match_count integer;
  v_backup_id bigint;
  v_snapshot jsonb;
BEGIN
  IF p_reset_source NOT IN ('manual', 'auto') THEN
    RETURN json_build_object('error', 'invalid_source');
  END IF;
  IF p_batch_id !~ '^[0-9a-f]{8,32}$' THEN
    RETURN json_build_object('error', 'invalid_batch_id');
  END IF;
  SELECT count(*)::integer INTO v_match_count
  FROM public.api_keys
  WHERE name = p_key_name AND deleted_at IS NULL;
  IF v_match_count = 0 THEN
    RETURN json_build_object('error', 'not_found');
  ELSIF v_match_count > 1 THEN
    RETURN json_build_object('error', 'duplicate_key_name');
  END IF;
  SELECT * INTO v_key
  FROM public.api_keys
  WHERE name = p_key_name AND deleted_at IS NULL
  FOR UPDATE;
  SELECT backup_id, snapshot INTO v_backup_id, v_snapshot
  FROM sub2api_tg_bot_api.rate_limit_backups
  WHERE api_key_id = v_key.id AND batch_id = p_batch_id
  ORDER BY created_at ASC, backup_id ASC
  LIMIT 1;
  IF FOUND THEN
    RETURN json_build_object(
      'backup_id', v_backup_id,
      'key_id', v_key.id,
      'key_name', v_key.name,
      'reset_source', p_reset_source,
      'batch_id', p_batch_id,
      'snapshot', v_snapshot
    );
  END IF;
  v_snapshot := jsonb_build_object(
    'usage_5h', v_key.usage_5h,
    'usage_1d', v_key.usage_1d,
    'usage_7d', v_key.usage_7d,
    'last_used_at', v_key.last_used_at,
    'rate_limit_7d', v_key.rate_limit_7d,
    'window_5h_start', v_key.window_5h_start,
    'window_1d_start', v_key.window_1d_start,
    'window_7d_start', v_key.window_7d_start
  );
  INSERT INTO sub2api_tg_bot_api.rate_limit_backups (
    api_key_id, key_name, account_id, reset_source, batch_id, snapshot
  ) VALUES (
    v_key.id, v_key.name, p_account_id, p_reset_source, p_batch_id, v_snapshot
  ) RETURNING backup_id INTO v_backup_id;
  DELETE FROM sub2api_tg_bot_api.rate_limit_backups
  WHERE api_key_id = v_key.id
    AND backup_id NOT IN (
      SELECT backup_id
      FROM sub2api_tg_bot_api.rate_limit_backups
      WHERE api_key_id = v_key.id
      ORDER BY created_at DESC, backup_id DESC
      LIMIT 3
    );
  RETURN json_build_object(
    'backup_id', v_backup_id,
    'key_id', v_key.id,
    'key_name', v_key.name,
    'reset_source', p_reset_source,
    'batch_id', p_batch_id,
    'snapshot', v_snapshot
  );
END;
$function$;

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.rate_limit_backup_batches(
  p_key_names jsonb
)
RETURNS json
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH requested_keys AS (
  SELECT DISTINCT value AS key_name
  FROM jsonb_array_elements_text(
    CASE WHEN jsonb_typeof(p_key_names) = 'array' THEN p_key_names ELSE '[]'::jsonb END
  )
), requested_count AS (
  SELECT count(*)::integer AS total FROM requested_keys
), matching_keys AS (
  SELECT api_key.id, api_key.name
  FROM public.api_keys AS api_key
  JOIN requested_keys AS requested ON requested.key_name = api_key.name
  WHERE api_key.deleted_at IS NULL
), match_count AS (
  SELECT count(*)::integer AS total FROM matching_keys
), eligible_batches AS (
  SELECT
    backup.batch_id,
    min(backup.created_at) AS created_at,
    min(backup.reset_source) AS reset_source,
    count(DISTINCT backup.api_key_id)::integer AS key_count
  FROM sub2api_tg_bot_api.rate_limit_backups AS backup
  JOIN matching_keys AS matching ON matching.id = backup.api_key_id
  WHERE backup.batch_id IS NOT NULL
  GROUP BY backup.batch_id
  HAVING count(DISTINCT backup.api_key_id) = (SELECT total FROM requested_count)
     AND count(DISTINCT backup.reset_source) = 1
  ORDER BY min(backup.created_at) DESC, backup.batch_id DESC
  LIMIT 3
), batch_payloads AS (
  SELECT
    batch.batch_id,
    batch.created_at,
    batch.reset_source,
    batch.key_count,
    (
      SELECT json_agg(json_build_object(
        'backup_id', backup.backup_id,
        'key_name', backup.key_name,
        'account_id', backup.account_id,
        'snapshot', backup.snapshot
      ) ORDER BY backup.key_name)
      FROM sub2api_tg_bot_api.rate_limit_backups AS backup
      JOIN matching_keys AS matching ON matching.id = backup.api_key_id
      WHERE backup.batch_id = batch.batch_id
    ) AS backups
  FROM eligible_batches AS batch
)
SELECT CASE
  WHEN jsonb_typeof(p_key_names) <> 'array' OR (SELECT total FROM requested_count) = 0
    THEN json_build_object('error', 'invalid_key_names')
  WHEN (SELECT total FROM match_count) <> (SELECT total FROM requested_count)
    THEN json_build_object('error', 'binding_mismatch')
  ELSE json_build_object(
    'batches', coalesce((
      SELECT json_agg(row_to_json(batch_payloads) ORDER BY created_at DESC, batch_id DESC)
      FROM batch_payloads
    ), '[]'::json)
  )
END;
$function$;

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.rate_limit_backups(p_key_name text)
RETURNS json
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
WITH matching_keys AS (
  SELECT id
  FROM public.api_keys
  WHERE name = p_key_name AND deleted_at IS NULL
), match_count AS (
  SELECT count(*)::integer AS total FROM matching_keys
), backups AS (
  SELECT backup_id, reset_source, snapshot, created_at
  FROM sub2api_tg_bot_api.rate_limit_backups
  WHERE api_key_id = (SELECT id FROM matching_keys)
  ORDER BY created_at DESC, backup_id DESC
  LIMIT 3
)
SELECT CASE
  WHEN (SELECT total FROM match_count) = 0
    THEN json_build_object('error', 'not_found')
  WHEN (SELECT total FROM match_count) > 1
    THEN json_build_object('error', 'duplicate_key_name')
  ELSE json_build_object(
    'key_id', (SELECT id FROM matching_keys),
    'backups', coalesce(
      (SELECT json_agg(json_build_object(
        'backup_id', backup_id,
        'reset_source', reset_source,
        'created_at', created_at,
        'snapshot', snapshot
      )) FROM backups),
      '[]'::json
    )
  )
END;
$function$;

CREATE OR REPLACE FUNCTION sub2api_tg_bot_api.restore_rate_limit_backup(
  p_backup_id bigint,
  p_key_name text
)
RETURNS json
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog
AS $function$
DECLARE
  v_match_count integer;
  v_key_id bigint;
  v_backup sub2api_tg_bot_api.rate_limit_backups%ROWTYPE;
BEGIN
  SELECT count(*)::integer, min(id) INTO v_match_count, v_key_id
  FROM public.api_keys
  WHERE name = p_key_name AND deleted_at IS NULL;
  IF v_match_count = 0 THEN
    RETURN json_build_object('error', 'not_found');
  ELSIF v_match_count > 1 THEN
    RETURN json_build_object('error', 'duplicate_key_name');
  END IF;
  SELECT * INTO v_backup
  FROM sub2api_tg_bot_api.rate_limit_backups
  WHERE backup_id = p_backup_id AND api_key_id = v_key_id;
  IF NOT FOUND THEN
    RETURN json_build_object('error', 'backup_not_found');
  END IF;
  UPDATE public.api_keys
  SET usage_5h = (v_backup.snapshot->>'usage_5h')::numeric,
      usage_1d = (v_backup.snapshot->>'usage_1d')::numeric,
      usage_7d = (v_backup.snapshot->>'usage_7d')::numeric,
      window_5h_start = (v_backup.snapshot->>'window_5h_start')::timestamptz,
      window_1d_start = (v_backup.snapshot->>'window_1d_start')::timestamptz,
      window_7d_start = (v_backup.snapshot->>'window_7d_start')::timestamptz
  WHERE id = v_key_id;
  RETURN json_build_object(
    'backup_id', v_backup.backup_id,
    'key_id', v_key_id,
    'key_name', p_key_name,
    'restored_at', clock_timestamp(),
    'snapshot', v_backup.snapshot
  );
END;
$function$;

REVOKE ALL PRIVILEGES ON DATABASE sub2api FROM sub2api_tg_bot;
REVOKE ALL PRIVILEGES ON TABLE public.api_keys, public.usage_logs, public.accounts FROM sub2api_tg_bot;
REVOKE CREATE ON SCHEMA public FROM sub2api_tg_bot;
REVOKE ALL ON SCHEMA sub2api_tg_bot_api FROM sub2api_tg_bot;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.usage(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.key_overview(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.key_ip_history(text, integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.usage_with_account(text, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.account_estimate(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.account_weekly_reset(bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.backup_rate_limits(text, bigint, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.rate_limit_backups(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.rate_limit_backup_batches(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION sub2api_tg_bot_api.restore_rate_limit_backup(bigint, text) FROM PUBLIC;

GRANT CONNECT ON DATABASE sub2api TO sub2api_tg_bot;
GRANT USAGE ON SCHEMA sub2api_tg_bot_api TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.usage(text) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.key_overview(text) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.key_ip_history(text, integer, integer) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.usage_with_account(text, bigint) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.account_estimate(bigint) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.account_weekly_reset(bigint) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.backup_rate_limits(text, bigint, text, text) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.rate_limit_backups(text) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.rate_limit_backup_batches(jsonb) TO sub2api_tg_bot;
GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.restore_rate_limit_backup(bigint, text) TO sub2api_tg_bot;
