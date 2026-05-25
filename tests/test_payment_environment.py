import hashlib
import hmac
import json
import os
import unittest
from unittest import mock

TEST_EMAIL_PREFIX = "codex-payment-test-"

os.environ["APP_ENV"] = "test"
os.environ["PAYMENT_MODE"] = "test"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_key"
os.environ["RAZORPAY_KEY_SECRET"] = "rzp_test_secret"
os.environ["PAYMENT_WEBHOOK_SECRET"] = "whsec_test"
os.environ["BOT_USERNAME"] = "QuizPathshala_bot"

from db.database import database
from services.payment_service_db import payment_service
from services.user_service_db import user_service
from services.web_admin_service import web_admin_service
from services.web_identity_service import web_identity_service
from services.web_payment_service import web_payment_service
from webhook_server import app


class _FakeOrderResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return dict(self._payload)


class _FakeHttpxClient:
    def __init__(self, response_payload: dict, *args, **kwargs):
        self._response_payload = response_payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, json: dict):
        return _FakeOrderResponse(self._response_payload)


class PaymentEnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database._initialized = False
        database.initialize()

    def setUp(self):
        with database.connection() as conn:
            conn.execute("DELETE FROM processed_webhooks WHERE event_id LIKE 'evt_test_%'")
            conn.execute("DELETE FROM payments WHERE payment_id LIKE 'pay_test_%'")
            conn.execute("DELETE FROM payment_orders WHERE order_id LIKE 'order_test_%'")
            conn.execute("DELETE FROM users WHERE login_identifier LIKE ?", (f"{TEST_EMAIL_PREFIX}%",))

    def _create_user(self, *, role: str = "user", email: str) -> dict:
        user = user_service.upsert_login_account(
            login_identifier=email,
            password="password123",
            full_name=email.split("@", 1)[0].title(),
            role=role,
        )
        if not user:
            user = user_service.find_by_login_identifier(email)
        self.assertTrue(user, f"Expected test user to exist for {email}")
        return user

    def _login(self, client, user: dict):
        snapshot = web_identity_service._build_user_snapshot(user)
        with client.session_transaction() as session:
            session[web_identity_service.AUTH_USER_KEY] = int(user["user_id"])
            session[web_identity_service.ROLE_KEY] = str(user.get("user_role") or "user")
            session[web_identity_service.SESSION_KEY] = int(user["user_id"])
            session[web_identity_service.SNAPSHOT_KEY] = snapshot
            if user.get("is_admin") or str(user.get("user_role")) in {"admin", "super_admin"}:
                session[web_identity_service.ADMIN_KEY] = True

    def _order_response(self, *, order_id: str, amount: int, user_id: int, plan_type: str) -> dict:
        return {
            "id": order_id,
            "amount": amount,
            "currency": "INR",
            "status": "created",
            "notes": {
                "user_id": str(user_id),
                "plan_type": plan_type,
            },
        }

    def _payment_signature(self, order_id: str, payment_id: str) -> str:
        return hmac.new(
            b"rzp_test_secret",
            f"{order_id}|{payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _webhook_signature(self, raw_body: bytes) -> str:
        return hmac.new(
            b"whsec_test",
            raw_body,
            hashlib.sha256,
        ).hexdigest()

    def test_test_order_creates_successfully(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-create@example.com")
        response_payload = self._order_response(
            order_id="order_test_create_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            order = web_payment_service.create_order(int(user["user_id"]), "month_1")

        self.assertEqual(order["order_id"], "order_test_create_001")
        saved_order = payment_service.get_order("order_test_create_001")
        self.assertIsNotNone(saved_order)
        self.assertEqual(saved_order["status"], "created")
        self.assertEqual(saved_order["plan_type"], "month_1")

    def test_payment_success_callback_updates_order_and_webhook_activates_premium(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-success@example.com")
        response_payload = self._order_response(
            order_id="order_test_success_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            created_order = web_payment_service.create_order(int(user["user_id"]), "month_1")

        payment_id = "pay_test_success_001"
        signature = self._payment_signature(created_order["order_id"], payment_id)

        with app.test_client() as client:
            self._login(client, user)
            callback_response = client.post(
                "/payment/success",
                data={
                    "razorpay_order_id": created_order["order_id"],
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": signature,
                },
            )

        self.assertEqual(callback_response.status_code, 200)
        callback_order = payment_service.get_order(created_order["order_id"])
        self.assertEqual(callback_order["status"], "callback_verified")
        self.assertFalse(bool(user_service.get_user(int(user["user_id"]))["is_premium"]))

        webhook_payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": created_order["order_id"],
                        "amount": 29900,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(webhook_payload).encode("utf-8")
        webhook_signature = self._webhook_signature(raw_body)

        webhook_response = app.test_client().post(
            "/webhook",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": webhook_signature,
                "X-Razorpay-Event-Id": "evt_test_success_001",
            },
        )

        self.assertEqual(webhook_response.status_code, 200)
        refreshed_user = user_service.get_user(int(user["user_id"]))
        self.assertTrue(bool(refreshed_user["is_premium"]))
        self.assertTrue(refreshed_user["premium_expires_at"])

        saved_order = payment_service.get_order(created_order["order_id"])
        self.assertEqual(saved_order["status"], "paid")

        with database.connection() as conn:
            payment_row = conn.execute(
                "SELECT * FROM payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
        self.assertIsNotNone(payment_row)
        self.assertEqual(payment_row["status"], "captured")

    def test_failed_payment_does_not_activate_premium(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-failed@example.com")
        response_payload = self._order_response(
            order_id="order_test_failed_001",
            amount=9900,
            user_id=int(user["user_id"]),
            plan_type="week_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            created_order = web_payment_service.create_order(int(user["user_id"]), "week_1")

        with app.test_client() as client:
            self._login(client, user)
            failed_response = client.get(
                "/payment/failed",
                query_string={
                    "razorpay_order_id": created_order["order_id"],
                    "reason": "payment_failed",
                },
            )

        self.assertEqual(failed_response.status_code, 200)
        refreshed_user = user_service.get_user(int(user["user_id"]))
        self.assertFalse(bool(refreshed_user["is_premium"]))
        saved_order = payment_service.get_order(created_order["order_id"])
        self.assertEqual(saved_order["status"], "failed")

    def test_admin_payment_logs_show_saved_entry(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-admin-log@example.com")
        response_payload = self._order_response(
            order_id="order_test_admin_001",
            amount=79900,
            user_id=int(user["user_id"]),
            plan_type="months_3",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            created_order = web_payment_service.create_order(int(user["user_id"]), "month_3")

        webhook_payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test_admin_001",
                        "order_id": created_order["order_id"],
                        "amount": 79900,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(webhook_payload).encode("utf-8")

        webhook_response = app.test_client().post(
            "/webhook",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": self._webhook_signature(raw_body),
                "X-Razorpay-Event-Id": "evt_test_admin_001",
            },
        )
        self.assertEqual(webhook_response.status_code, 200)

        with mock.patch.object(web_admin_service, "list_admin_plans", return_value=[]), mock.patch.object(
            web_admin_service, "list_set_choices", return_value=[]
        ), mock.patch.object(web_admin_service, "list_exams_overview", return_value=[]), mock.patch.object(
            web_admin_service, "search_questions", return_value=[]
        ):
            dashboard = web_admin_service.dashboard_page_data()

        payment_ids = {item["payment_id"] for item in dashboard["payments"]}
        order_ids = {item["order_id"] for item in dashboard["orders"]}
        self.assertIn("pay_test_admin_001", payment_ids)
        self.assertIn(created_order["order_id"], order_ids)

    def test_non_test_payment_mode_is_blocked(self):
        with mock.patch("services.payment_service_db.PAYMENT_MODE", "live"):
            self.assertIn("PAYMENT_MODE=test", payment_service.get_missing_configuration())
            with self.assertRaisesRegex(ValueError, "PAYMENT_MODE=test"):
                payment_service.validate_test_mode()


if __name__ == "__main__":
    unittest.main()
