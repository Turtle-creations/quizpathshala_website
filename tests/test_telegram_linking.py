import os
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse


TEST_EMAIL_PREFIX = "codex-telegram-link-test-"
TEST_TELEGRAM_IDS = (990000001, 990000002, 990000003)

os.environ["APP_ENV"] = "test"
os.environ["BOT_USERNAME"] = "QuizPathshala_bot"

from db.database import database
from services.telegram_link_service import telegram_link_service
from services.user_service_db import user_service
from services.web_identity_service import web_identity_service
from webhook_server import app


class TelegramLinkingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database._initialized = False
        database.initialize()

    def setUp(self):
        with database.connection() as conn:
            conn.execute("DELETE FROM telegram_link_tokens")
            conn.execute("DELETE FROM telegram_account_links")
            for table_name in (
                "payment_orders",
                "payments",
                "quiz_attempts",
                "question_reports",
                "support_messages",
                "password_reset_requests",
            ):
                placeholders = ", ".join("?" for _ in TEST_TELEGRAM_IDS)
                conn.execute(f"DELETE FROM {table_name} WHERE user_id IN ({placeholders})", TEST_TELEGRAM_IDS)
            placeholders = ", ".join("?" for _ in TEST_TELEGRAM_IDS)
            conn.execute(f"DELETE FROM users WHERE user_id IN ({placeholders})", TEST_TELEGRAM_IDS)
            conn.execute("DELETE FROM users WHERE login_identifier LIKE ?", (f"{TEST_EMAIL_PREFIX}%",))

    def _create_user(self, suffix: str) -> dict:
        email = f"{TEST_EMAIL_PREFIX}{suffix}@example.com"
        return user_service.upsert_login_account(
            login_identifier=email,
            password="password123",
            full_name=suffix.replace("-", " ").title(),
            role="user",
        )

    def _login(self, client, user: dict):
        snapshot = web_identity_service._build_user_snapshot(user)
        with client.session_transaction() as session:
            session[web_identity_service.AUTH_USER_KEY] = int(user["user_id"])
            session[web_identity_service.ROLE_KEY] = str(user.get("user_role") or "user")
            session[web_identity_service.SESSION_KEY] = int(user["user_id"])
            session[web_identity_service.SNAPSHOT_KEY] = snapshot

    def _tg_user(self, user_id: int, *, username: str = "telegramstudent", full_name: str = "Telegram Student"):
        return SimpleNamespace(id=user_id, username=username, full_name=full_name, first_name=full_name.split(" ", 1)[0])

    def test_dashboard_link_route_redirects_to_bot_start_url(self):
        user = self._create_user("dashboard-route")

        with app.test_client() as client:
            self._login(client, user)
            response = client.post("/dashboard/telegram-link")

        self.assertEqual(response.status_code, 302)
        location = response.headers["Location"]
        self.assertIn("https://t.me/QuizPathshala_bot?start=", location)
        start_token = parse_qs(urlparse(location).query).get("start", [""])[0]
        self.assertTrue(start_token)

        with database.connection() as conn:
            token_row = conn.execute("SELECT * FROM telegram_link_tokens").fetchone()
        self.assertIsNotNone(token_row)
        self.assertEqual(int(token_row["website_user_id"]), int(user["user_id"]))
        self.assertFalse(token_row["used_at"])

    def test_linking_keeps_website_name_and_merges_account_stats(self):
        website_user = self._create_user("merge-account")
        telegram_user_id = TEST_TELEGRAM_IDS[0]

        user_service.ensure_profile(
            user_id=telegram_user_id,
            full_name="Telegram Only",
            username="merge_me",
        )
        user_service.record_quiz_start(telegram_user_id)
        user_service.record_answer(telegram_user_id, correct=True)

        token_result = telegram_link_service.create_link_request(int(website_user["user_id"]))
        consume_result = telegram_link_service.consume_start_token(
            token_result["token"],
            self._tg_user(telegram_user_id, username="merged_user", full_name="Merged User"),
        )

        self.assertTrue(consume_result["ok"])
        linked_row = telegram_link_service.get_website_link(int(website_user["user_id"]))
        self.assertEqual(int(linked_row["telegram_user_id"]), telegram_user_id)

        resolved_user = user_service.ensure_user(self._tg_user(telegram_user_id, username="merged_user", full_name="Merged User"))
        self.assertEqual(int(resolved_user["user_id"]), int(website_user["user_id"]))

        refreshed_website_user = user_service.get_user(int(website_user["user_id"]))
        self.assertEqual(refreshed_website_user["website_name"], "Merge Account")
        self.assertEqual(refreshed_website_user["full_name"], "Merge Account")
        self.assertEqual(refreshed_website_user["username"], "merged_user")
        self.assertEqual(refreshed_website_user["telegram_username"], "merged_user")
        self.assertEqual(refreshed_website_user["telegram_first_name"], "Merged")
        self.assertEqual(refreshed_website_user["telegram_full_name"], "Merged User")
        self.assertEqual(int(refreshed_website_user["quiz_played"]), 1)
        self.assertEqual(int(refreshed_website_user["correct_answers"]), 1)
        self.assertAlmostEqual(float(refreshed_website_user["score"]), 1.0)
        self.assertFalse(user_service.get_user(telegram_user_id))

    def test_link_token_is_one_time_and_expiring(self):
        website_user = self._create_user("token-rules")
        telegram_user = self._tg_user(TEST_TELEGRAM_IDS[1], username="token_rules", full_name="Token Rules")

        token_result = telegram_link_service.create_link_request(int(website_user["user_id"]))
        first_use = telegram_link_service.consume_start_token(token_result["token"], telegram_user)
        second_use = telegram_link_service.consume_start_token(token_result["token"], telegram_user)

        self.assertTrue(first_use["ok"])
        self.assertFalse(second_use["ok"])
        self.assertEqual(second_use["status"], "used_token")

        expired_result = telegram_link_service.create_link_request(int(website_user["user_id"]))
        token_hash = telegram_link_service._hash_token(expired_result["token"])
        with database.connection() as conn:
            conn.execute(
                "UPDATE telegram_link_tokens SET expires_at = ? WHERE token_hash = ?",
                ("2000-01-01T00:00:00+00:00", token_hash),
            )

        expired_use = telegram_link_service.consume_start_token(
            expired_result["token"],
            self._tg_user(TEST_TELEGRAM_IDS[2], username="expired_user", full_name="Expired User"),
        )
        self.assertFalse(expired_use["ok"])
        self.assertEqual(expired_use["status"], "expired_token")

    def test_existing_link_requires_confirmation_before_replacing(self):
        first_website_user = self._create_user("first-link")
        second_website_user = self._create_user("second-link")
        telegram_user = self._tg_user(TEST_TELEGRAM_IDS[0], username="sharedtg", full_name="Shared Telegram")

        initial_token = telegram_link_service.create_link_request(int(first_website_user["user_id"]))
        initial_link = telegram_link_service.consume_start_token(initial_token["token"], telegram_user)
        self.assertTrue(initial_link["ok"])

        replacement_token = telegram_link_service.create_link_request(int(second_website_user["user_id"]))
        replacement_attempt = telegram_link_service.consume_start_token(replacement_token["token"], telegram_user)
        self.assertFalse(replacement_attempt["ok"])
        self.assertEqual(replacement_attempt["status"], "confirmation_required")
        self.assertEqual(
            int(telegram_link_service.get_telegram_link(TEST_TELEGRAM_IDS[0])["website_user_id"]),
            int(first_website_user["user_id"]),
        )

        confirmed_token = telegram_link_service.create_link_request(
            int(second_website_user["user_id"]),
            allow_relink=True,
        )
        confirmed_link = telegram_link_service.consume_start_token(confirmed_token["token"], telegram_user)
        self.assertTrue(confirmed_link["ok"])
        self.assertEqual(
            int(telegram_link_service.get_telegram_link(TEST_TELEGRAM_IDS[0])["website_user_id"]),
            int(second_website_user["user_id"]),
        )


if __name__ == "__main__":
    unittest.main()
