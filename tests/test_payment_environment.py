import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
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
            amount=9900,
            user_id=int(user["user_id"]),
            plan_type="week_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            order = web_payment_service.create_order(int(user["user_id"]), "week_1")

        self.assertEqual(order["order_id"], "order_test_create_001")
        saved_order = payment_service.get_order("order_test_create_001")
        self.assertIsNotNone(saved_order)
        self.assertEqual(saved_order["status"], "created")
        self.assertEqual(saved_order["plan_type"], "week_1")
        self.assertTrue(saved_order["updated_at"])

    def test_payment_order_upsert_updates_existing_row(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-upsert@example.com")
        payment_service._save_order_record(
            order_id="order_test_upsert_001",
            user_id=int(user["user_id"]),
            plan_type="week_1",
            amount=9900,
            currency="INR",
            status="created",
            payment_url="/payment/order_test_upsert_001",
        )
        payment_service._save_order_record(
            order_id="order_test_upsert_001",
            user_id=int(user["user_id"]),
            plan_type="week_1",
            amount=9900,
            currency="INR",
            status="callback_verified",
            payment_url="/payment/order_test_upsert_001",
        )

        saved_order = payment_service.get_order("order_test_upsert_001")
        self.assertIsNotNone(saved_order)
        self.assertEqual(saved_order["status"], "callback_verified")
        self.assertTrue(saved_order["updated_at"])

    def test_premium_page_enables_checkout_in_test_mode_with_checkout_keys(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-premium-page@example.com")

        with app.test_client() as client:
            self._login(client, user)
            with mock.patch.object(payment_service, "get_checkout_blockers", return_value=[]), mock.patch.object(
                payment_service, "get_missing_configuration", return_value=["PAYMENT_WEBHOOK_SECRET is missing"]
            ):
                response = client.get("/premium")

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Continue to Payment", body)
        self.assertIn("Real UPI apps can fail in Razorpay test mode", body)
        self.assertIn("Razorpay test cards", body)
        self.assertNotIn("Checkout Unavailable", body)

    def test_premium_page_shows_exact_disabled_reason(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-disabled-page@example.com")

        with app.test_client() as client:
            self._login(client, user)
            with mock.patch.object(
                payment_service,
                "get_checkout_blockers",
                return_value=["PAYMENT_MODE must be set to test", "RAZORPAY_KEY_SECRET is missing"],
            ), mock.patch.object(
                payment_service,
                "get_missing_configuration",
                return_value=["PAYMENT_MODE must be set to test", "RAZORPAY_KEY_SECRET is missing"],
            ):
                response = client.get("/premium")

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Test checkout disabled", body)
        self.assertIn("PAYMENT_MODE must be set to test", body)
        self.assertIn("RAZORPAY_KEY_SECRET is missing", body)

    def test_payment_page_does_not_auto_open_checkout(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-no-auto-open@example.com")
        response_payload = self._order_response(
            order_id="order_test_no_auto_open_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with app.test_client() as client:
            self._login(client, user)
            with mock.patch(
                "services.web_payment_service.httpx.Client",
                side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
            ):
                response = client.post("/premium", data={"plan_type": "month_1"}, follow_redirects=True)

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Pay Now", body)
        self.assertIn("real UPI apps may fail", body)
        self.assertIn("/payment/cancel?razorpay_order_id=order_test_no_auto_open_001", body)
        self.assertNotIn("window.addEventListener(\"load\"", body)

    def test_payment_client_event_marks_checkout_opened_after_click(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-click-open@example.com")
        response_payload = self._order_response(
            order_id="order_test_click_open_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            web_payment_service.create_order(int(user["user_id"]), "month_1")

        with app.test_client() as client:
            self._login(client, user)
            response = client.post(
                "/payment/order_test_click_open_001/client-event",
                json={"event": "razorpay_checkout_opened"},
            )

        self.assertEqual(response.status_code, 200)
        saved_order = payment_service.get_order("order_test_click_open_001")
        self.assertTrue(saved_order["checkout_opened_at"])

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
        callback_body = callback_response.get_data(as_text=True)
        self.assertIn("Payment progress", callback_body)
        self.assertIn("Payment submitted, waiting for confirmation.", callback_body)
        self.assertIn("Callback received", callback_body)
        self.assertIn("Webhook received", callback_body)
        callback_order = payment_service.get_order(created_order["order_id"])
        self.assertEqual(callback_order["status"], "callback_verified")
        self.assertEqual(int(callback_order["callback_verified"] or 0), 1)
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
            "/payment/webhook",
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
        self.assertEqual(int(saved_order["webhook_verified"] or 0), 1)
        self.assertTrue(saved_order["premium_activated_at"])
        self.assertEqual(saved_order["webhook_event_type"], "payment.captured")

    def test_webhook_verified_captured_status_page_is_not_pending(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-webhook-status@example.com")
        response_payload = self._order_response(
            order_id="order_test_webhook_status_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            created_order = web_payment_service.create_order(int(user["user_id"]), "month_1")

        webhook_payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test_webhook_status_001",
                        "order_id": created_order["order_id"],
                        "amount": 29900,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(webhook_payload).encode("utf-8")
        webhook_response = app.test_client().post(
            "/payment/webhook",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": self._webhook_signature(raw_body),
                "X-Razorpay-Event-Id": "evt_test_webhook_status_001",
            },
        )
        self.assertEqual(webhook_response.status_code, 200)

        saved_order = payment_service.get_order(created_order["order_id"])
        self.assertEqual(saved_order["status"], "paid")
        self.assertEqual(saved_order["payment_status"], "captured")
        self.assertEqual(saved_order["current_step"] if "current_step" in saved_order else payment_service.current_step(saved_order), "premium_activated")

        with app.test_client() as client:
            self._login(client, user)
            status_response = client.get(f"/payment/status/{created_order['order_id']}")

        self.assertEqual(status_response.status_code, 200)
        status_body = status_response.get_data(as_text=True)
        self.assertIn("Premium has been activated.", status_body)
        self.assertNotIn("Payment submitted, waiting for confirmation.", status_body)

    def test_existing_premium_user_is_extended_once_after_captured_webhook(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-extended@example.com")
        existing_expiry = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0).isoformat()
        user_service.set_premium_expiry(int(user["user_id"]), existing_expiry, True)
        response_payload = self._order_response(
            order_id="order_test_extended_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            created_order = web_payment_service.create_order(int(user["user_id"]), "month_1")

        webhook_payload = {
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_test_extended_001",
                        "order_id": created_order["order_id"],
                        "amount": 29900,
                        "currency": "INR",
                        "status": "captured",
                    }
                }
            },
        }
        raw_body = json.dumps(webhook_payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Razorpay-Signature": self._webhook_signature(raw_body),
        }

        first_response = app.test_client().post(
            "/payment/webhook",
            data=raw_body,
            headers={**headers, "X-Razorpay-Event-Id": "evt_test_extended_001"},
        )
        self.assertEqual(first_response.status_code, 200)

        first_user = user_service.get_user(int(user["user_id"]))
        first_expiry = first_user["premium_expires_at"]
        self.assertGreater(datetime.fromisoformat(first_expiry), datetime.fromisoformat(existing_expiry))

        saved_order = payment_service.get_order(created_order["order_id"])
        self.assertEqual(saved_order["status"], "paid")
        self.assertEqual(saved_order["payment_status"], "captured")
        self.assertEqual(saved_order["premium_result"], "extended")
        self.assertEqual(payment_service.current_step(saved_order), "premium_extended")

        duplicate_response = app.test_client().post(
            "/payment/webhook",
            data=raw_body,
            headers={**headers, "X-Razorpay-Event-Id": "evt_test_extended_002"},
        )
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(duplicate_response.get_json()["status"], "already_processed")

        second_user = user_service.get_user(int(user["user_id"]))
        self.assertEqual(second_user["premium_expires_at"], first_expiry)

    def test_duplicate_payment_captured_webhook_returns_safe_200_without_double_activation(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-webhook-duplicate@example.com")
        response_payload = self._order_response(
            order_id="order_test_duplicate_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            created_order = web_payment_service.create_order(int(user["user_id"]), "month_1")

        payment_id = "pay_test_duplicate_001"
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

        first_response = app.test_client().post(
            "/payment/webhook",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": webhook_signature,
                "X-Razorpay-Event-Id": "evt_test_duplicate_001",
            },
        )
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.get_json()["status"], "processed")

        first_user = user_service.get_user(int(user["user_id"]))
        self.assertTrue(bool(first_user["is_premium"]))
        first_expiry = first_user["premium_expires_at"]
        self.assertTrue(first_expiry)

        duplicate_response = app.test_client().post(
            "/payment/webhook",
            data=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": webhook_signature,
                "X-Razorpay-Event-Id": "evt_test_duplicate_002",
            },
        )
        self.assertEqual(duplicate_response.status_code, 200)
        duplicate_payload = duplicate_response.get_json()
        self.assertEqual(duplicate_payload["status"], "already_processed")
        self.assertEqual(duplicate_payload["reason"], "payment_exists")

        second_user = user_service.get_user(int(user["user_id"]))
        self.assertTrue(bool(second_user["is_premium"]))
        self.assertEqual(second_user["premium_expires_at"], first_expiry)

        with database.connection() as conn:
            payment_rows = conn.execute(
                "SELECT COUNT(*) AS count FROM payments WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            processed_rows = conn.execute(
                "SELECT duplicate_count FROM processed_webhooks WHERE event_id = ?",
                ("evt_test_duplicate_002",),
            ).fetchone()

        self.assertEqual(int(payment_rows["count"]), 1)
        self.assertIsNotNone(processed_rows)
        self.assertEqual(int(processed_rows["duplicate_count"] or 0), 1)

    def test_stale_verified_captured_order_is_reconciled_from_pending(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-reconcile@example.com")
        payment_service._save_order_record(
            order_id="order_test_reconcile_001",
            user_id=int(user["user_id"]),
            plan_type="month_1",
            amount=29900,
            currency="INR",
            status="pending",
            payment_url="/payment/order_test_reconcile_001",
        )
        payment_service._update_order_debug(
            "order_test_reconcile_001",
            payment_id="pay_test_reconcile_001",
            webhook_verified=1,
            webhook_status="verified",
            webhook_event_type="payment.captured",
            webhook_received_at="2026-01-01T00:00:00+00:00",
            payment_status="pending",
        )

        reconciled_order = payment_service.get_order("order_test_reconcile_001")
        self.assertEqual(reconciled_order["status"], "paid")
        self.assertEqual(reconciled_order["payment_status"], "captured")
        self.assertTrue(reconciled_order["premium_activated_at"])
        self.assertEqual(payment_service.current_step(reconciled_order), "premium_activated")

    def test_posting_plan_redirects_to_manual_payment_page(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-open-checkout@example.com")
        response_payload = self._order_response(
            order_id="order_test_open_checkout_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with app.test_client() as client:
            self._login(client, user)
            with mock.patch(
                "services.web_payment_service.httpx.Client",
                side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
            ):
                response = client.post("/premium", data={"plan_type": "month_1"}, follow_redirects=True)

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("https://checkout.razorpay.com/v1/checkout.js", body)
        self.assertIn("pay-btn", body)
        self.assertIn("rzp.open();", body)
        self.assertNotIn("window.addEventListener(\"load\"", body)
        self.assertIn("Pay Now", body)
        saved_order = payment_service.get_order("order_test_open_checkout_001")
        self.assertFalse(bool(saved_order["checkout_opened_at"]))

    def test_cancelled_checkout_marks_order_cancelled(self):
        user = self._create_user(email=f"{TEST_EMAIL_PREFIX}student-cancelled@example.com")
        response_payload = self._order_response(
            order_id="order_test_cancelled_001",
            amount=29900,
            user_id=int(user["user_id"]),
            plan_type="month_1",
        )

        with mock.patch(
            "services.web_payment_service.httpx.Client",
            side_effect=lambda *args, **kwargs: _FakeHttpxClient(response_payload, *args, **kwargs),
        ):
            created_order = web_payment_service.create_order(int(user["user_id"]), "month_1")

        with app.test_client() as client:
            self._login(client, user)
            cancelled_response = client.get(
                "/payment/cancel",
                query_string={"razorpay_order_id": created_order["order_id"]},
            )

        self.assertEqual(cancelled_response.status_code, 200)
        cancelled_body = cancelled_response.get_data(as_text=True)
        self.assertIn("Payment Cancelled", cancelled_body)
        self.assertIn("checkout was closed before payment confirmation", cancelled_body)
        saved_order = payment_service.get_order(created_order["order_id"])
        self.assertEqual(saved_order["status"], "cancelled")
        self.assertEqual(saved_order["callback_status"], "cancelled")
        self.assertEqual(saved_order["failure_reason"], "checkout_cancelled")

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
                    "razorpay_payment_id": "pay_test_failed_001",
                    "reason": "payment_failed",
                },
            )

        self.assertEqual(failed_response.status_code, 200)
        failed_body = failed_response.get_data(as_text=True)
        self.assertIn("Payment progress", failed_body)
        self.assertIn("Failed", failed_body)
        refreshed_user = user_service.get_user(int(user["user_id"]))
        self.assertFalse(bool(refreshed_user["is_premium"]))
        saved_order = payment_service.get_order(created_order["order_id"])
        self.assertEqual(saved_order["status"], "failed")
        self.assertEqual(saved_order["payment_id"], "pay_test_failed_001")
        self.assertEqual(saved_order["failure_reason"], "payment_failed")
        self.assertEqual(saved_order["last_error"], "payment_failed")
        self.assertEqual(saved_order["error_reason"], "payment_failed")
        self.assertEqual(int(saved_order["callback_verified"] or 0), 0)
        self.assertEqual(saved_order["callback_status"], "failed")

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
            "/payment/webhook",
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
        matching_order = next(item for item in dashboard["orders"] if item["order_id"] == created_order["order_id"])
        self.assertIn("pay_test_admin_001", payment_ids)
        self.assertIn(created_order["order_id"], order_ids)
        self.assertEqual(matching_order["payment_id"], "pay_test_admin_001")
        self.assertEqual(matching_order["status"], "paid")
        self.assertEqual(matching_order["payment_status"], "captured")
        self.assertEqual(int(matching_order["callback_verified"] or 0), 0)
        self.assertEqual(int(matching_order["webhook_verified"] or 0), 1)
        self.assertEqual(matching_order["current_step"], "premium_activated")
        self.assertEqual(matching_order["webhook_status"], "verified")
        self.assertEqual(matching_order["webhook_event_type"], "payment.captured")
        self.assertTrue(matching_order["updated_at"])

    def test_non_test_payment_mode_is_blocked(self):
        with mock.patch("services.payment_service_db.PAYMENT_MODE", "live"), mock.patch(
            "services.payment_service_db.PAYMENT_LIVE_ENABLED", False
        ):
            self.assertIn("PAYMENT_MODE must be set to test", payment_service.get_missing_configuration())
            with self.assertRaisesRegex(ValueError, "Live payment is disabled for this build"):
                payment_service.validate_test_mode()


if __name__ == "__main__":
    unittest.main()
