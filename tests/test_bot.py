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
            UPDATE_WORKERS=4,
            UPDATE_MAX_PENDING=16,
            CHECK_COOLDOWN=10,
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


class DataSafetyTests(unittest.TestCase):
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
        ), mock.patch.object(bot.subprocess, "check_output", return_value='{"ok": true}\n') as check:
            result = bot.run_psql_json("SELECT :'key_name';", {"key_name": "example-key"})
        self.assertEqual(result, {"ok": True})
        command = check.call_args.args[0]
        environment = check.call_args.kwargs["env"]
        self.assertNotIn("docker", command)
        self.assertIn("--set=key_name=example-key", command)
        self.assertEqual(environment["PGUSER"], "sub2api_tg_bot")
        self.assertIn("default_transaction_read_only=on", environment["PGOPTIONS"])
        self.assertNotIn("TELEGRAM_BOT_TOKEN", environment)

    def test_database_setup_exposes_only_fixed_function(self):
        with open("deploy/create_readonly_role.sql", "r", encoding="utf-8") as file:
            sql = file.read()
        self.assertIn("SECURITY DEFINER", sql)
        self.assertIn("SET search_path = pg_catalog", sql)
        self.assertIn("REVOKE ALL PRIVILEGES ON TABLE public.api_keys, public.usage_logs", sql)
        self.assertIn("GRANT EXECUTE ON FUNCTION sub2api_tg_bot_api.usage(text)", sql)
        self.assertNotIn("GRANT SELECT", sql)

    def test_alert_state_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "alert_state.json")
            with mock.patch.object(bot, "ALERT_STATE_PATH", path):
                bot.save_alert_state({"example": {"alerted_at": 1}})
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)


class ContainerPackagingTests(unittest.TestCase):
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
        self.assertNotIn("docker.sock", compose)
        self.assertNotIn("ports:", compose)
        self.assertNotIn("env_file:", compose)
        self.assertNotIn("WEBHOOK", compose)
        self.assertNotIn("PUBLIC_WEBHOOK", compose)


class DispatcherTests(unittest.TestCase):
    def test_duplicate_and_busy_updates_are_bounded(self):
        started = threading.Event()
        release = threading.Event()

        def blocked_handler(_message):
            started.set()
            release.wait(timeout=2)

        with mock.patch.object(bot, "handle_message", blocked_handler):
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
        dispatcher.submit.assert_called_once_with(10, {"text": "/start"})

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

    def test_non_message_update_is_acknowledged(self):
        dispatcher = mock.Mock()
        offset, busy = bot.dispatch_update_batch([{"update_id": 20, "callback_query": {}}], dispatcher)
        self.assertEqual(offset, 21)
        self.assertFalse(busy)
        dispatcher.submit.assert_not_called()

    def test_poll_loop_uses_get_updates_and_stops_cleanly(self):
        dispatcher = mock.Mock()
        stop_event = threading.Event()

        def fake_tg(method, params, timeout):
            stop_event.set()
            self.assertEqual(method, "getUpdates")
            self.assertEqual(params["timeout"], "7")
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
