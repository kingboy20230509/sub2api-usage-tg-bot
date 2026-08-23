import http.client
import json
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
            WEBHOOK_SECRET="A" * 32,
            WEBHOOK_PATH="/tg-sub2api-bot/" + "B" * 32,
            PUBLIC_WEBHOOK_URL="https://bot.example/tg-sub2api-bot/" + "B" * 32,
            MAX_WEBHOOK_BODY=65536,
            WEBHOOK_WORKERS=4,
            WEBHOOK_MAX_PENDING=16,
            CHECK_COOLDOWN=10,
        )

    def test_valid_runtime_configuration(self):
        with self.valid_runtime():
            bot.validate_runtime_config()

    def test_empty_webhook_secret_is_rejected(self):
        with self.valid_runtime(), mock.patch.object(bot, "WEBHOOK_SECRET", ""):
            with self.assertRaisesRegex(RuntimeError, "WEBHOOK_SECRET"):
                bot.validate_runtime_config()

    def test_placeholder_webhook_secret_is_rejected(self):
        with self.valid_runtime(), mock.patch.object(bot, "WEBHOOK_SECRET", "replace_me_with_a_long_random_string"):
            with self.assertRaisesRegex(RuntimeError, "WEBHOOK_SECRET"):
                bot.validate_runtime_config()

    def test_webhook_url_path_must_match(self):
        with self.valid_runtime(), mock.patch.object(bot, "PUBLIC_WEBHOOK_URL", "https://bot.example/wrong"):
            with self.assertRaisesRegex(RuntimeError, "PUBLIC_WEBHOOK_URL"):
                bot.validate_runtime_config()


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

    def test_alert_state_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "alert_state.json")
            with mock.patch.object(bot, "ALERT_STATE_PATH", path):
                bot.save_alert_state({"example": {"alerted_at": 1}})
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)


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


@unittest.skipUnless(os.environ.get("RUN_NETWORK_TESTS") == "1", "set RUN_NETWORK_TESTS=1 to bind a loopback test server")
class WebhookHandlerTests(unittest.TestCase):
    def setUp(self):
        self.runtime = mock.patch.multiple(
            bot,
            WEBHOOK_SECRET="A" * 32,
            WEBHOOK_PATH="/tg-sub2api-bot/" + "B" * 32,
            MAX_WEBHOOK_BODY=128,
        )
        self.runtime.start()
        self.server = bot.ThreadingHTTPServer(("127.0.0.1", 0), bot.Handler)
        self.server.daemon_threads = True
        self.server.dispatcher = mock.Mock()
        self.server.dispatcher.submit.return_value = "accepted"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.runtime.stop()

    def request(self, body, secret=None, content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        headers = {"Content-Type": content_type}
        if secret is not None:
            headers["X-Telegram-Bot-Api-Secret-Token"] = secret
        connection.request("POST", bot.WEBHOOK_PATH, body=body, headers=headers)
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
        return status

    def test_wrong_secret_is_forbidden(self):
        self.assertEqual(self.request(b"{}", secret="wrong"), 403)
        self.server.dispatcher.submit.assert_not_called()

    def test_oversized_request_is_rejected(self):
        self.assertEqual(self.request(b"x" * 129, secret="A" * 32), 413)
        self.server.dispatcher.submit.assert_not_called()

    def test_invalid_json_is_rejected(self):
        self.assertEqual(self.request(b"not-json", secret="A" * 32), 400)
        self.server.dispatcher.submit.assert_not_called()

    def test_valid_update_is_dispatched(self):
        body = json.dumps({"update_id": 123, "message": {"text": "/start"}}).encode()
        self.assertEqual(self.request(body, secret="A" * 32), 200)
        self.server.dispatcher.submit.assert_called_once_with(123, {"text": "/start"})


if __name__ == "__main__":
    unittest.main()
