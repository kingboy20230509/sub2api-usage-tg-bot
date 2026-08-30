import http.client
import os
import stat
import tempfile
import threading
import unittest
from unittest import mock

import sub2api_tg_bot as bot


class RuntimeConfigTests(unittest.TestCase):
    def valid_runtime(self):
        return mock.patch.multiple(
            bot,
            TOKEN="123456:valid-token",
            PSQL_BIN="/usr/bin/true",
            PGHOST="127.0.0.1",
            PGPORT="5432",
            PGDATABASE="sub2api",
            PGUSER="sub2api_tg_bot",
            PGPASSWORD="a-valid-restricted-password",
            PGSSLMODE="prefer",
            PG_ALLOW_INSECURE_PRIVATE_NETWORK="0",
            SUB2API_BASE_URL="",
            SUB2API_ADMIN_API_KEY="",
            SUB2API_ADMIN_TIMEOUT=10,
            UPDATE_WORKERS=4,
            UPDATE_MAX_PENDING=16,
            CHECK_COOLDOWN=10,
            ADMIN_CHECK_COOLDOWN=2,
            POLL_TIMEOUT=10,
        )

    def test_valid_runtime_configuration(self):
        with self.valid_runtime():
            bot.validate_runtime_config()

    def test_poll_timeout_is_bounded(self):
        with self.valid_runtime(), mock.patch.object(bot, "POLL_TIMEOUT", 0):
            with self.assertRaisesRegex(RuntimeError, "POLL_TIMEOUT"):
                bot.validate_runtime_config()

    def test_remote_database_requires_tls(self):
        with self.valid_runtime(), mock.patch.multiple(bot, PGHOST="db.example", PGSSLMODE="prefer"):
            with self.assertRaisesRegex(RuntimeError, "Remote PostgreSQL must use TLS"):
                bot.validate_runtime_config()

    def test_compose_service_can_use_explicit_private_network(self):
        with self.valid_runtime(), mock.patch.multiple(
            bot,
            PGHOST="sub2api-postgres",
            PGSSLMODE="disable",
            PG_ALLOW_INSECURE_PRIVATE_NETWORK="1",
        ):
            bot.validate_runtime_config()

    def test_private_network_exception_rejects_public_hostname(self):
        with self.valid_runtime(), mock.patch.multiple(
            bot,
            PGHOST="db.example.com",
            PGSSLMODE="disable",
            PG_ALLOW_INSECURE_PRIVATE_NETWORK="1",
        ):
            with self.assertRaisesRegex(RuntimeError, "trusted private network"):
                bot.validate_runtime_config()

    def test_reset_api_config_must_be_configured_as_a_pair(self):
        with self.valid_runtime(), mock.patch.multiple(
            bot,
            SUB2API_BASE_URL="http://sub2api:8080",
            SUB2API_ADMIN_API_KEY="",
        ):
            with self.assertRaisesRegex(RuntimeError, "configured together"):
                bot.validate_runtime_config()

    def test_reset_api_rejects_plain_http_public_host(self):
        with self.valid_runtime(), mock.patch.multiple(
            bot,
            SUB2API_BASE_URL="http://sub2api.example.com",
            SUB2API_ADMIN_API_KEY="valid-admin-secret",
        ):
            with self.assertRaisesRegex(RuntimeError, "private Compose network"):
                bot.validate_runtime_config()

    def test_reset_api_accepts_private_compose_service(self):
        with self.valid_runtime(), mock.patch.multiple(
            bot,
            SUB2API_BASE_URL="http://sub2api:8080",
            SUB2API_ADMIN_API_KEY="valid-admin-secret",
        ):
            bot.validate_runtime_config()

    def test_secret_can_be_read_from_absolute_file(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as secret_file:
            secret_file.write("secret-value\n")
            secret_file.flush()
            with mock.patch.dict(os.environ, {"EXAMPLE_SECRET_FILE": secret_file.name}, clear=False):
                self.assertEqual(bot.read_secret("EXAMPLE_SECRET"), "secret-value")

    def test_secret_rejects_environment_and_file_together(self):
        with mock.patch.dict(
            os.environ,
            {"EXAMPLE_SECRET": "value", "EXAMPLE_SECRET_FILE": "/run/secrets/example"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "Set only one"):
                bot.read_secret("EXAMPLE_SECRET")


class MessageAuthorizationTests(unittest.TestCase):
    def setUp(self):
        bot._LAST_CHECK_BY_USER.clear()

    def test_only_matching_private_chat_is_authorized(self):
        self.assertTrue(bot.is_private_user_chat({"id": 123, "type": "private"}, {"id": 123}))
        self.assertFalse(bot.is_private_user_chat({"id": -10, "type": "group"}, {"id": 123}))
        self.assertFalse(bot.is_private_user_chat({"id": 999, "type": "private"}, {"id": 123}))

    @mock.patch.object(bot, "query_key_usage")
    @mock.patch.object(bot, "tg")
    def test_group_check_never_queries_database(self, tg, query):
        bot.handle_message({
            "chat": {"id": -100, "type": "group"},
            "from": {"id": 123},
            "text": "/check",
        })
        query.assert_not_called()
        tg.assert_called_once()
        self.assertIn("私聊", tg.call_args.args[1]["text"])

    @mock.patch.object(bot, "query_key_usage")
    @mock.patch.object(bot, "tg")
    def test_forged_private_chat_id_never_queries_database(self, tg, query):
        bot.handle_message({
            "chat": {"id": 999, "type": "private"},
            "from": {"id": 123},
            "text": "/check",
        })
        query.assert_not_called()
        tg.assert_called_once()

    def test_per_user_check_cooldown(self):
        with mock.patch.object(bot, "CHECK_COOLDOWN", 10):
            self.assertEqual(bot.allow_check("123", now=100), (True, 0))
            allowed, retry_after = bot.allow_check("123", now=101)
            self.assertFalse(allowed)
            self.assertEqual(retry_after, 9)
            self.assertEqual(bot.allow_check("123", now=111), (True, 0))

    def test_admin_check_can_use_a_shorter_cooldown(self):
        self.assertEqual(bot.allow_check("123", now=100, cooldown=2), (True, 0))
        self.assertEqual(bot.allow_check("123", now=101, cooldown=2), (False, 1))
        self.assertEqual(bot.allow_check("123", now=102, cooldown=2), (True, 0))

    def test_bindings_accept_account_id_objects_and_legacy_strings(self):
        bindings = bot.config_bindings({
            "bindings": {
                "123": {"key_name": "Administrator", "account_id": 12},
                "456": "legacy-key",
            },
        })
        self.assertEqual(bindings["123"], {"key_name": "Administrator", "account_id": 12})
        self.assertEqual(bindings["456"], {"key_name": "legacy-key", "account_id": None})

    def test_binding_rejects_invalid_account_id(self):
        with self.assertRaisesRegex(ValueError, "positive integer"):
            bot.config_bindings({
                "bindings": {"123": {"key_name": "Administrator", "account_id": "12"}},
            })

    @mock.patch.object(bot, "query_key_usage")
    @mock.patch.object(bot, "tg")
    def test_admin_check_shows_two_column_key_menu_without_querying(self, tg, query):
        config = {
            "admins": [123],
            "bindings": {
                "123": "Administrator",
                "456": "User A",
                "789": "User B",
            },
        }
        with mock.patch.object(bot, "load_config", return_value=config):
            bot.handle_message({
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123},
                "text": "/check",
            })
        query.assert_not_called()
        params = tg.call_args.args[1]
        self.assertEqual(params["text"], "请选择要查看的 Key：")
        keyboard = bot.json.loads(params["reply_markup"])["inline_keyboard"]
        self.assertEqual([len(row) for row in keyboard], [2, 1])
        buttons = [button for row in keyboard for button in row]
        self.assertEqual({button["text"] for button in buttons}, {"Administrator", "User A", "User B"})
        self.assertEqual({button["callback_data"] for button in buttons}, {"usage:123", "usage:456", "usage:789"})

    @mock.patch.object(bot, "query_key_usage", return_value={"error": "not_found"})
    @mock.patch.object(bot, "tg")
    def test_regular_user_check_queries_only_own_binding(self, tg, query):
        config = {
            "admins": [123],
            "bindings": {"123": "Administrator", "456": "User A"},
        }
        with mock.patch.object(bot, "load_config", return_value=config):
            bot.handle_message({
                "chat": {"id": 456, "type": "private"},
                "from": {"id": 456},
                "text": "/check",
            })
        query.assert_called_once_with("User A", None)
        self.assertEqual(tg.call_args.args[0], "sendMessage")

    @mock.patch.object(bot, "query_key_usage")
    @mock.patch.object(bot, "tg")
    def test_non_admin_cannot_use_admin_callback(self, tg, query):
        config = {
            "admins": [123],
            "bindings": {"123": "Administrator", "456": "User A"},
        }
        with mock.patch.object(bot, "load_config", return_value=config):
            bot.handle_callback_query({
                "id": "callback-1",
                "from": {"id": 456},
                "data": "usage:123",
                "message": {"message_id": 8, "chat": {"id": 456, "type": "private"}},
            })
        query.assert_not_called()
        self.assertEqual(tg.call_args.args[0], "answerCallbackQuery")
        self.assertIn("没有管理员权限", tg.call_args.args[1]["text"])

    @mock.patch.object(bot, "reset_key_rate_limit_usage")
    @mock.patch.object(bot, "query_key_usage")
    @mock.patch.object(bot, "tg")
    def test_non_admin_cannot_forge_reset_callback(self, tg, query, reset):
        config = {
            "admins": [123],
            "bindings": {"123": "Administrator", "456": "User A"},
        }
        with mock.patch.object(bot, "load_config", return_value=config):
            bot.handle_callback_query({
                "id": "callback-reset-forged",
                "from": {"id": 456},
                "data": "reset_confirm:123",
                "message": {"message_id": 8, "chat": {"id": 456, "type": "private"}},
            })
        query.assert_not_called()
        reset.assert_not_called()
        self.assertIn("没有管理员权限", tg.call_args.args[1]["text"])

    @mock.patch.object(bot, "query_key_usage", return_value={"error": "not_found"})
    @mock.patch.object(bot, "tg")
    def test_admin_callback_queries_selected_binding_and_keeps_menu(self, tg, query):
        config = {
            "admins": [123],
            "bindings": {"123": "Administrator", "456": "User A"},
        }
        with mock.patch.object(bot, "load_config", return_value=config):
            bot.handle_callback_query({
                "id": "callback-2",
                "from": {"id": 123},
                "data": "usage:456",
                "message": {"message_id": 9, "chat": {"id": 123, "type": "private"}},
            })
        query.assert_called_once_with("User A", None)
        self.assertEqual([call.args[0] for call in tg.call_args_list], ["answerCallbackQuery", "editMessageText"])
        edit = tg.call_args_list[1].args[1]
        self.assertEqual(edit["message_id"], 9)
        self.assertIn("未找到绑定的 key：User A", edit["text"])
        self.assertIn("🔄 刷新时间：", edit["text"])
        self.assertIn("inline_keyboard", bot.json.loads(edit["reply_markup"]))

    @mock.patch.object(bot, "format_refresh_timestamp", side_effect=[
        "2026-08-27 10:00:00",
        "2026-08-27 10:00:10",
    ])
    @mock.patch.object(bot, "allow_check", return_value=(True, 0))
    @mock.patch.object(bot, "query_key_usage", return_value={"error": "not_found"})
    @mock.patch.object(bot, "tg")
    def test_admin_can_refresh_the_same_binding(self, tg, query, _allow, _refresh_time):
        config = {
            "admins": [123],
            "bindings": {"123": "Administrator", "456": "User A"},
            "timezone": "Asia/Shanghai",
        }
        callback = {
            "id": "callback-refresh",
            "from": {"id": 123},
            "data": "usage:456",
            "message": {"message_id": 10, "chat": {"id": 123, "type": "private"}},
        }
        with mock.patch.object(bot, "load_config", return_value=config):
            bot.handle_callback_query(callback)
            bot.handle_callback_query(callback)
        self.assertEqual(query.call_count, 2)
        self.assertEqual(
            _allow.call_args_list,
            [
                mock.call("123", cooldown=bot.ADMIN_CHECK_COOLDOWN),
                mock.call("123", cooldown=bot.ADMIN_CHECK_COOLDOWN),
            ],
        )
        edits = [call.args[1]["text"] for call in tg.call_args_list if call.args[0] == "editMessageText"]
        self.assertEqual(len(edits), 2)
        self.assertNotEqual(edits[0], edits[1])
        self.assertIn("刷新时间：2026-08-27 10:00:10", edits[1])

    @mock.patch.object(bot, "query_key_usage")
    @mock.patch.object(bot, "reset_key_rate_limit_usage")
    @mock.patch.object(bot, "tg")
    def test_admin_reset_requires_confirmation(self, tg, reset, query):
        config = {"admins": [123], "bindings": {"456": "User A"}}
        with mock.patch.object(bot, "load_config", return_value=config), mock.patch.multiple(
            bot,
            SUB2API_BASE_URL="http://sub2api:8080",
            SUB2API_ADMIN_API_KEY="admin-secret",
        ):
            bot.handle_callback_query({
                "id": "callback-reset-prompt",
                "from": {"id": 123},
                "data": "reset_prompt:456",
                "message": {"message_id": 11, "chat": {"id": 123, "type": "private"}},
            })
        query.assert_not_called()
        reset.assert_not_called()
        edit = tg.call_args_list[1].args[1]
        self.assertIn("确认重置", edit["text"])
        buttons = bot.json.loads(edit["reply_markup"])["inline_keyboard"][0]
        self.assertEqual(
            {button["callback_data"] for button in buttons},
            {"reset_confirm:456", "reset_cancel:456"},
        )

    @mock.patch.object(bot, "format_refresh_timestamp", return_value="2026-08-30 12:00:00")
    @mock.patch.object(bot, "reset_key_rate_limit_usage", return_value={"success": True})
    @mock.patch.object(bot, "query_key_usage", side_effect=[
        {"key": {
            "id": 42, "name": "User A",
            "rate_limit_5h": 10, "rate_limit_1d": 20, "rate_limit_7d": 30,
            "usage_5h": 5, "usage_1d": 6, "usage_7d": 7,
        }},
        {"key": {
            "id": 42, "name": "User A",
            "rate_limit_5h": 10, "rate_limit_1d": 20, "rate_limit_7d": 30,
            "usage_5h": 0, "usage_1d": 0, "usage_7d": 0,
        }},
    ])
    @mock.patch.object(bot, "tg")
    def test_admin_reset_rechecks_usage(self, tg, query, reset, _refresh):
        config = {"admins": [123], "bindings": {"456": "User A"}}
        with mock.patch.object(bot, "load_config", return_value=config), mock.patch.multiple(
            bot,
            SUB2API_BASE_URL="http://sub2api:8080",
            SUB2API_ADMIN_API_KEY="admin-secret",
        ):
            bot.handle_callback_query({
                "id": "callback-reset-confirm",
                "from": {"id": 123},
                "data": "reset_confirm:456",
                "message": {"message_id": 12, "chat": {"id": 123, "type": "private"}},
            })
        self.assertEqual(query.call_count, 2)
        reset.assert_called_once_with(42)
        edit = tg.call_args_list[-1].args[1]
        self.assertIn("已重置并完成复查", edit["text"])
        self.assertIn("5 小时：已用 0 / 限额 10", edit["text"])
        self.assertIn("每日：已用 0 / 限额 20", edit["text"])
        self.assertIn("每周：已用 0 / 限额 30", edit["text"])
        self.assertIn("复查时间：2026-08-30 12:00:00", edit["text"])
        buttons = [
            button
            for row in bot.json.loads(edit["reply_markup"])["inline_keyboard"]
            for button in row
        ]
        self.assertIn("reset_prompt:456", {button["callback_data"] for button in buttons})

    @mock.patch.object(bot, "reset_key_rate_limit_usage", return_value={"success": True})
    @mock.patch.object(bot, "query_key_usage", side_effect=[
        {"key": {"id": 42, "name": "User A"}},
        RuntimeError("database unavailable"),
    ])
    @mock.patch.object(bot, "tg")
    def test_successful_reset_reports_recheck_failure_separately(self, tg, query, reset):
        config = {"admins": [123], "bindings": {"456": "User A"}}
        with mock.patch.object(bot, "load_config", return_value=config), mock.patch.multiple(
            bot,
            SUB2API_BASE_URL="http://sub2api:8080",
            SUB2API_ADMIN_API_KEY="admin-secret",
        ):
            bot.handle_callback_query({
                "id": "callback-reset-recheck-failure",
                "from": {"id": 123},
                "data": "reset_confirm:456",
                "message": {"message_id": 13, "chat": {"id": 123, "type": "private"}},
            })
        reset.assert_called_once_with(42)
        self.assertIn("重置请求已成功，但复查失败", tg.call_args.args[1]["text"])


class DataSafetyTests(unittest.TestCase):
    def test_reset_api_uses_admin_header_and_refuses_redirects(self):
        response = mock.MagicMock()
        response.read.return_value = b'{"success":true}'
        context = mock.MagicMock()
        context.__enter__.return_value = response
        opener = mock.MagicMock()
        opener.open.return_value = context
        with mock.patch.multiple(
            bot,
            SUB2API_BASE_URL="http://sub2api:8080",
            SUB2API_ADMIN_API_KEY="admin-secret",
            SUB2API_ADMIN_TIMEOUT=7,
        ), mock.patch.object(bot.urllib.request, "build_opener", return_value=opener) as build_opener:
            result = bot.reset_key_rate_limit_usage(42)
        self.assertEqual(result, {"success": True})
        self.assertIsInstance(build_opener.call_args.args[0], bot.NoRedirectHandler)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://sub2api:8080/api/v1/admin/api-keys/42")
        self.assertEqual(request.method, "PUT")
        self.assertEqual(request.get_header("X-api-key"), "admin-secret")
        self.assertEqual(bot.json.loads(request.data), {"reset_rate_limit_usage": True})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)

    def test_reset_api_rejects_invalid_key_id_before_network(self):
        with mock.patch.object(bot.urllib.request, "build_opener") as build_opener:
            for value in (True, 0, -1, "42"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    bot.reset_key_rate_limit_usage(value)
            build_opener.assert_not_called()

    def test_invalid_key_name_is_rejected_before_subprocess(self):
        with mock.patch.object(bot, "run_psql_json") as run:
            with self.assertRaises(ValueError):
                bot.query_key_usage("bad'; DROP TABLE api_keys; --")
            run.assert_not_called()

    def test_valid_key_uses_psql_variable(self):
        with mock.patch.object(bot, "run_psql_json", return_value={}) as run:
            bot.query_key_usage("example-key")
        run.assert_called_once_with(
            "SELECT sub2api_tg_bot_api.usage(:'key_name')::text;",
            {"key_name": "example-key"},
        )

    def test_account_binding_uses_fixed_account_snapshot_function(self):
        with mock.patch.object(bot, "run_psql_json", return_value={}) as run:
            bot.query_key_usage("example-key", 12)
        run.assert_called_once_with(
            "SELECT sub2api_tg_bot_api.usage_with_account(:'key_name', :'account_id'::bigint)::text;",
            {"key_name": "example-key", "account_id": "12"},
        )

    def test_invalid_account_id_is_rejected_before_subprocess(self):
        for value in (True, 0, -1, "12"):
            with self.subTest(value=value), mock.patch.object(bot, "run_psql_json") as run:
                with self.assertRaises(ValueError):
                    bot.query_key_usage("example-key", value)
                run.assert_not_called()

    def test_psql_subprocess_gets_only_database_environment(self):
        with mock.patch.multiple(
            bot,
            PSQL_BIN="/usr/bin/psql",
            PGHOST="127.0.0.1",
            PGPORT="5432",
            PGDATABASE="sub2api",
            PGUSER="sub2api_tg_bot",
            PGPASSWORD="database-secret",
            PGSSLMODE="prefer",
            TOKEN="telegram-secret",
            SUB2API_ADMIN_API_KEY="sub2api-admin-secret",
        ), mock.patch.object(bot.subprocess, "check_output", return_value='{"ok": true}\n') as check:
            result = bot.run_psql_json("SELECT :'key_name';", {"key_name": "example-key"})
        self.assertEqual(result, {"ok": True})
        command = check.call_args.args[0]
        stdin_sql = check.call_args.kwargs["input"]
        environment = check.call_args.kwargs["env"]
        self.assertNotIn("docker", command)
        self.assertIn("--set=key_name=example-key", command)
        self.assertIn("--file", command)
        self.assertIn("-", command)
        self.assertNotIn("--command", command)
        self.assertEqual(stdin_sql, "SELECT :'key_name';")
        self.assertEqual(environment["PGUSER"], "sub2api_tg_bot")
        self.assertIn("default_transaction_read_only=on", environment["PGOPTIONS"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", environment)
        self.assertNotIn("SUB2API_ADMIN_API_KEY", environment)

    def test_database_setup_exposes_only_fixed_function(self):
        with open("deploy/create_readonly_role.sql", "r", encoding="utf-8") as file:
            sql = file.read()
        self.assertIn("SECURITY DEFINER", sql)
        self.assertIn("SET search_path = pg_catalog", sql)
        self.assertIn("REVOKE ALL PRIVILEGES ON TABLE public.api_keys, public.usage_logs, public.accounts", sql)
        self.assertIn("GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.usage(text)", sql)
        self.assertIn("GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.usage_with_account(text, bigint)", sql)
        self.assertIn("FROM public.accounts", sql)
        self.assertIn("extra->>'codex_usage_updated_at'", sql)
        self.assertIn("extra->>'codex_5h_reset_at'", sql)
        self.assertIn("extra->>'codex_7d_reset_at'", sql)
        self.assertIn("'models_today'", sql)
        self.assertIn("'models_7d'", sql)
        self.assertIn("interval '6 days'", sql)
        self.assertIn("AS window_start", sql)
        self.assertIn("AS window_end", sql)
        self.assertIn("sum(cache_creation_tokens)", sql)
        self.assertIn("sum(cache_read_tokens)", sql)
        self.assertIn("match_count AS", sql)
        self.assertIn("'duplicate_key_name'", sql)
        self.assertNotIn("ORDER BY id ASC", sql)
        self.assertNotIn("GRANT SELECT", sql)

    def test_duplicate_key_name_returns_a_safe_error(self):
        output = bot.format_usage("same-name", {"error": "duplicate_key_name"})
        self.assertIn("检测到多个同名 Key：same-name", output)
        self.assertIn("名称修改为唯一名称", output)

    def test_usage_output_has_progress_time_and_model_cache_details(self):
        data = {
            "key": {
                "name": "example-key",
                "status": "active",
                "quota": 100,
                "quota_used": 25,
                "rate_limit_5h": 100,
                "usage_5h": 25,
                "rate_limit_7d": 600,
                "usage_7d": 25,
                "window_7d_start": "2026-08-24T00:00:00+00:00",
                "window_7d_end": "2026-08-31T00:00:00+00:00",
                "last_used_at": "2026-08-21T15:31:41.722896+08:00",
            },
            "today": {
                "requests": 1,
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_creation_tokens": 3,
                "cache_read_tokens": 4,
                "actual_cost": "0.01",
            },
            "seven_days": {
                "requests": 2,
                "input_tokens": 20,
                "output_tokens": 4,
                "cache_creation_tokens": 6,
                "cache_read_tokens": 8,
                "actual_cost": "0.02",
                "window_start": "2026-08-19T16:00:00+00:00",
                "window_end": "2026-08-25T16:00:00+00:00",
            },
            "models_today": [{
                "model": "gpt-today",
                "requests": 1,
                "input_tokens": 10,
                "output_tokens": 2,
                "cache_creation_tokens": 3,
                "cache_read_tokens": 4,
                "actual_cost": "0.01",
            }],
            "models_7d": [{
                "model": "gpt-all",
                "requests": 2,
                "input_tokens": 20,
                "output_tokens": 4,
                "cache_creation_tokens": 6,
                "cache_read_tokens": 8,
                "actual_cost": "0.02",
            }],
            "upstream_account": {
                "id": 12,
                "platform": "openai",
                "type": "codex",
                "snapshot_updated_at": "2026-08-24T06:59:30Z",
                "reset_5h_at": "2026-08-24T12:30:00Z",
                "reset_7d_at": "2026-08-31T00:00:00Z",
            },
        }
        output = bot.format_usage(
            "example-key",
            data,
            now=bot.datetime.fromisoformat("2026-08-24T07:00:00+00:00"),
        )
        self.assertIn("• 5 小时：已用 25 / 限额 100 / 剩余 75", output)
        self.assertIn("重置时间：2026-08-24 20:30:00｜剩余：5h 30m", output)
        self.assertIn("重置时间：2026-08-31 08:00:00｜剩余：6d 17h", output)
        self.assertNotIn("上游账号：", output)
        self.assertNotIn("快照更新：", output)
        self.assertNotIn("重置周期：", output)
        self.assertIn("  [███░░░░░░░░░] 25%", output)
        self.assertEqual(output.count("["), 2)
        self.assertNotIn("💰 额度", output)
        self.assertNotIn("总额", output)
        self.assertIn("状态：正常", output)
        self.assertIn("2026-08-21 15:31:41", output)
        self.assertIn("今日模型 Top 5", output)
        self.assertIn("7天模型 Top 5", output)
        self.assertIn("gpt-today", output)
        self.assertIn("gpt-all", output)
        self.assertIn("📊 7天用量", output)
        self.assertIn("统计范围：2026-08-20 ～ 2026-08-26", output)
        self.assertIn("━━━━━━━━━━━━━━━━", output)
        self.assertIn("Tokens：输入 0.02k / 输出 0.004k", output)
        self.assertIn("缓存占比：读取 23.53%｜写入 17.65%", output)
        self.assertLess(len(output), 4096)

    def test_missing_configured_account_is_visible_without_guessing_a_reset(self):
        data = {
            "key": {"name": "example-key", "status": "active"},
            "upstream_account": {"error": "not_found", "id": 99},
        }
        output = bot.format_usage("example-key", data)
        self.assertIn("未找到配置的上游账号 ID：99", output)
        self.assertNotIn("重置时间：", output)

    def test_unlimited_window_does_not_show_its_upstream_reset(self):
        data = {
            "key": {
                "name": "example-key",
                "status": "active",
                "rate_limit_5h": 0,
                "rate_limit_7d": 600,
            },
            "upstream_account": {
                "id": 2,
                "reset_5h_at": "2026-08-24T12:30:00Z",
                "reset_7d_at": "2026-08-31T00:00:00Z",
            },
        }
        output = bot.format_usage(
            "example-key",
            data,
            now=bot.datetime.fromisoformat("2026-08-24T07:00:00+00:00"),
        )
        self.assertIn("• 5 小时：不限", output)
        self.assertNotIn("2026-08-24 20:30:00", output)
        self.assertIn("重置时间：2026-08-31 08:00:00｜剩余：6d 17h", output)

    def test_upstream_reset_supports_unix_seconds_and_expired_snapshots(self):
        reset_at = bot.datetime.fromisoformat("2026-08-24T07:00:00+00:00").timestamp()
        now = bot.datetime.fromisoformat("2026-08-24T07:00:01+00:00")
        self.assertEqual(bot.reset_remaining_text(int(reset_at), now), "等待快照更新")
        lines = []
        bot.append_account_reset(lines, int(reset_at), now)
        self.assertEqual(lines, ["  重置时间：2026-08-24 15:00:00｜剩余：等待快照更新"])

    def test_numbers_use_at_most_two_decimal_places(self):
        self.assertEqual(bot.money("0.000468"), "0")
        self.assertEqual(bot.money("0.05398224"), "0.05")
        self.assertEqual(bot.money("0.125653"), "0.13")
        self.assertEqual(bot.money("12.50"), "12.5")
        self.assertEqual(bot.money("100.00"), "100")

    def test_cache_percentages_hide_raw_token_counts(self):
        values = {
            "input_tokens": 248597,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 304128,
        }
        self.assertEqual(bot.cache_percentage_text(values), "读取 55.02%｜写入 0%")
        self.assertEqual(bot.cache_percentage_text({}), "暂无数据")

    def test_token_counts_use_compact_units(self):
        self.assertEqual(bot.format_tokens(4), "0.004k")
        self.assertEqual(bot.format_tokens(531), "0.53k")
        self.assertEqual(bot.format_tokens(618561), "618.6k")
        self.assertEqual(bot.format_tokens(1250000), "1.25M")
        self.assertEqual(bot.format_tokens(1200000000), "1.2B")

    def test_alert_state_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "alert_state.json")
            with mock.patch.object(bot, "ALERT_STATE_PATH", path):
                bot.save_alert_state({"example": {"alerted_at": 1}})
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)


class ContainerPackagingTests(unittest.TestCase):
    def test_example_config_binds_key_names_to_account_ids(self):
        with open("config.example.json", "r", encoding="utf-8") as file:
            config = bot.json.load(file)
        bindings = bot.config_bindings(config)
        self.assertEqual(bindings["123456789"], {"key_name": "Administrator", "account_id": 12})

    def test_image_runs_as_unprivileged_user_and_has_healthcheck(self):
        with open("Dockerfile", "r", encoding="utf-8") as file:
            dockerfile = file.read()
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("postgresql-client", dockerfile)
        self.assertNotIn("docker.sock", dockerfile)

    def test_compose_does_not_publish_ports_or_mount_docker_socket(self):
        with open("compose.example.yaml", "r", encoding="utf-8") as file:
            compose = file.read()
        self.assertIn("read_only: true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn("internal: true", compose)
        self.assertIn("PG_ALLOW_INSECURE_PRIVATE_NETWORK: \"1\"", compose)
        self.assertIn("SUB2API_ADMIN_API_KEY_FILE: /run/secrets/sub2api_admin_api_key", compose)
        self.assertIn("SUB2API_BASE_URL:", compose)
        self.assertIn("file: ./secrets/sub2api_admin_api_key", compose)
        self.assertNotIn("docker.sock", compose)
        self.assertNotIn("ports:", compose)
        self.assertNotIn("env_file:", compose)
        self.assertNotIn("WEBHOOK", compose)
        self.assertNotIn("PUBLIC_WEBHOOK", compose)


class DispatcherTests(unittest.TestCase):
    def test_duplicate_and_busy_updates_are_bounded(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_handler(_update):
            started.set()
            release.wait(timeout=2)

        with mock.patch.object(bot, "handle_update", blocked_handler):
            dispatcher = bot.UpdateDispatcher(workers=1, max_pending=1)
            try:
                self.assertEqual(dispatcher.submit(1, {}), "accepted")
                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(dispatcher.submit(1, {}), "duplicate")
                self.assertEqual(dispatcher.submit(2, {}), "busy")
            finally:
                release.set()
                dispatcher.shutdown()

    def test_poll_batch_advances_offset_after_accepted_update(self):
        dispatcher = mock.Mock()
        dispatcher.submit.return_value = "accepted"
        offset, busy = bot.dispatch_update_batch(
            [{"update_id": 10, "message": {"text": "/start"}}],
            dispatcher,
        )
        self.assertEqual(offset, 11)
        self.assertFalse(busy)
        dispatcher.submit.assert_called_once_with(10, {"update_id": 10, "message": {"text": "/start"}})

    def test_busy_poll_update_is_not_acknowledged(self):
        dispatcher = mock.Mock()
        dispatcher.submit.side_effect = ["accepted", "busy"]
        offset, busy = bot.dispatch_update_batch(
            [
                {"update_id": 10, "message": {"text": "/start"}},
                {"update_id": 11, "message": {"text": "/check"}},
            ],
            dispatcher,
        )
        self.assertEqual(offset, 11)
        self.assertTrue(busy)

    def test_callback_update_is_submitted(self):
        dispatcher = mock.Mock()
        dispatcher.submit.return_value = "accepted"
        offset, busy = bot.dispatch_update_batch([{"update_id": 20, "callback_query": {}}], dispatcher)
        self.assertEqual(offset, 21)
        self.assertFalse(busy)
        dispatcher.submit.assert_called_once_with(20, {"update_id": 20, "callback_query": {}})

    def test_poll_loop_uses_get_updates_and_stops_cleanly(self):
        dispatcher = mock.Mock()
        stop_event = threading.Event()

        def fake_tg(method, params, timeout):
            stop_event.set()
            self.assertEqual(method, "getUpdates")
            self.assertEqual(params["timeout"], "7")
            self.assertEqual(bot.json.loads(params["allowed_updates"]), ["message", "callback_query"])
            self.assertEqual(timeout, 12)
            return []

        with mock.patch.object(bot, "POLL_TIMEOUT", 7), mock.patch.object(bot, "tg", fake_tg):
            bot.poll_updates(dispatcher, stop_event)
        dispatcher.submit.assert_not_called()


@unittest.skipUnless(os.environ.get("RUN_NETWORK_TESTS") == "1", "set RUN_NETWORK_TESTS=1 to bind a loopback test server")
class HealthHandlerTests(unittest.TestCase):
    def setUp(self):
        self.server = bot.ThreadingHTTPServer(("127.0.0.1", 0), bot.Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request("GET", path)
        response = connection.getresponse()
        status = response.status
        body = response.read()
        connection.close()
        return status, body

    def test_health_endpoint(self):
        self.assertEqual(self.request("/health"), (200, b"ok"))

    def test_other_endpoint_is_not_found(self):
        self.assertEqual(self.request("/anything-else"), (404, b""))


if __name__ == "__main__":
    unittest.main()
