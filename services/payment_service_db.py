import os
import hashlib
import hmac
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone

import httpx

from config import (
    BOT_USERNAME,
    PAYMENT_MODE,
    PAYMENT_LIVE_ENABLED,
    PUBLIC_BASE_URL,
    RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET,
    RAZORPAY_WEBHOOK_SECRET,
)
from db.database import database
from services.user_service_db import now_iso, parse_utc_datetime, user_service
from utils.timezone_utils import format_user_datetime
from utils.logging_utils import get_logger


SUBSCRIPTION_PLANS = {
    "week_1": {"name": "1 Week", "amount": 9900, "days": 7},
    "month_1": {"name": "1 Month", "amount": 29900, "days": 30},
    "months_3": {"name": "3 Months", "amount": 79900, "days": 90},
    "year_1": {"name": "1 Year", "amount": 249900, "days": 365},
}

PREMIUM_PRICE_PLAN_ALIASES = {
    "week_1": "week_1",
    "month_1": "month_1",
    "month_3": "months_3",
    "months_3": "months_3",
}


logger = get_logger(__name__)
MAX_WEBHOOK_DUPLICATE_COUNT = 5
PREMIUM_ACTIVATION_SOURCE_WEBHOOK = "razorpay_webhook"
_UNSET = object()


class PaymentService:
    def is_test_mode(self) -> bool:
        return PAYMENT_MODE == "test"

    def is_live_mode(self) -> bool:
        return PAYMENT_MODE == "live"

    def live_checkout_enabled(self) -> bool:
        return self.is_live_mode() and PAYMENT_LIVE_ENABLED

    def checkout_mode_name(self) -> str:
        if self.is_test_mode():
            return "test"
        if self.live_checkout_enabled():
            return "live"
        if self.is_live_mode():
            return "live"
        return "disabled"

    def validate_payment_mode(self) -> None:
        if self.is_test_mode() or self.live_checkout_enabled():
            return
        if self.is_live_mode():
            raise ValueError(
                "Live payment is blocked until PAYMENT_LIVE_ENABLED=true is set together with PAYMENT_MODE=live."
            )
        raise ValueError(
            f"Payment mode '{PAYMENT_MODE or 'disabled'}' is not allowed. "
            "Set PAYMENT_MODE=test for test checkout or PAYMENT_MODE=live with PAYMENT_LIVE_ENABLED=true for live checkout."
        )

    def validate_test_mode(self) -> None:
        self.validate_payment_mode()

    def get_checkout_blockers(self) -> list[str]:
        blockers = []
        if self.is_test_mode():
            if not (RAZORPAY_KEY_ID or "").strip():
                blockers.append("RAZORPAY_KEY_ID is missing")
            if not (RAZORPAY_KEY_SECRET or "").strip():
                blockers.append("RAZORPAY_KEY_SECRET is missing")
            return blockers

        if self.live_checkout_enabled():
            if not (RAZORPAY_KEY_ID or "").strip():
                blockers.append("RAZORPAY_KEY_ID is missing")
            if not (RAZORPAY_KEY_SECRET or "").strip():
                blockers.append("RAZORPAY_KEY_SECRET is missing")
            if not (RAZORPAY_WEBHOOK_SECRET or "").strip():
                blockers.append("RAZORPAY_WEBHOOK_SECRET is missing")
            return blockers

        if self.is_live_mode():
            blockers.append("PAYMENT_LIVE_ENABLED must be true when PAYMENT_MODE=live")
        else:
            blockers.append("PAYMENT_MODE must be set to test or live")
        if not (RAZORPAY_KEY_ID or "").strip():
            blockers.append("RAZORPAY_KEY_ID is missing")
        if not (RAZORPAY_KEY_SECRET or "").strip():
            blockers.append("RAZORPAY_KEY_SECRET is missing")
        return blockers

    def payment_mode_note(self) -> str:
        if self.is_test_mode():
            return (
                "Test mode is active. Real UPI apps can fail in Razorpay test mode, "
                "so use Razorpay test cards or supported test methods while verifying the flow."
            )
        if self.live_checkout_enabled():
            return (
                "Live mode is active. Premium will activate only after a valid captured-payment webhook is verified on the server."
            )
        if self.is_live_mode() and not PAYMENT_LIVE_ENABLED:
            return (
                "Live mode was requested, but checkout is still blocked until PAYMENT_LIVE_ENABLED=true is set."
            )
        return "Payments are disabled until a valid payment mode is configured."

    def checkout_ready(self) -> bool:
        return not self.get_checkout_blockers()

    def _tracker_steps(self, order: dict) -> list[dict]:
        status_text = str(order.get("status") or "")
        callback_verified = bool(int(order.get("callback_verified") or 0))
        webhook_verified = bool(int(order.get("webhook_verified") or 0))
        payment_submitted = bool(order.get("payment_submitted_at"))
        callback_received = bool(order.get("callback_received_at"))
        webhook_received = bool(order.get("webhook_received_at"))
        payment_complete = self._is_payment_complete(order)
        premium_complete = self._is_premium_complete(order)
        premium_failure = self._has_premium_failure(order)
        terminal_failure = status_text in {"failed", "cancelled", "callback_signature_failed", "paid_activation_failed"} or premium_failure
        premium_label = "Premium extended" if str(order.get("premium_result") or "") == "extended" else "Premium activated"

        steps = [
            {"key": "order_created", "label": "Order created", "status": "success", "timestamp": order.get("created_at")},
            {
                "key": "checkout_opened",
                "label": "Razorpay checkout opened",
                "status": "success" if order.get("checkout_opened_at") or payment_complete else ("failed" if terminal_failure else "pending"),
                "timestamp": order.get("checkout_opened_at") or (order.get("webhook_received_at") if payment_complete else None),
            },
            {
                "key": "payment_submitted",
                "label": "Payment submitted",
                "status": "success" if payment_submitted or payment_complete else ("failed" if terminal_failure else "pending"),
                "timestamp": order.get("payment_submitted_at") or (order.get("webhook_received_at") if payment_complete else None),
            },
            {
                "key": "callback_received",
                "label": "Callback received",
                "status": "success" if (callback_received and callback_verified) or payment_complete else ("failed" if callback_received or terminal_failure else "pending"),
                "timestamp": order.get("callback_received_at") or (order.get("webhook_received_at") if payment_complete else None),
            },
            {
                "key": "webhook_received",
                "label": "Webhook received",
                "status": "success" if payment_complete else ("failed" if webhook_received and not webhook_verified else ("failed" if terminal_failure else "pending")),
                "timestamp": order.get("webhook_received_at") or (order.get("premium_activated_at") if payment_complete else None),
            },
            {
                "key": "premium_activated",
                "label": premium_label,
                "status": "success" if premium_complete else ("failed" if terminal_failure else "pending"),
                "timestamp": order.get("premium_activated_at") or (order.get("webhook_received_at") if payment_complete and not premium_failure else None),
            },
        ]
        return steps

    def current_step(self, order: dict) -> str:
        if self._is_payment_failed(order):
            return "payment_failed"
        if self._has_premium_failure(order):
            return "premium_activation_failed"
        if str(order.get("premium_result") or "") == "extended":
            return "premium_extended"
        if self._is_premium_complete(order):
            return "premium_activated"
        if str(order.get("webhook_status") or "") == "failed":
            return "webhook_failed"
        if order.get("webhook_received_at"):
            return "webhook_received"
        if str(order.get("callback_status") or "") == "failed":
            return "callback_failed"
        if order.get("callback_received_at"):
            return "callback_received"
        if order.get("payment_submitted_at"):
            return "payment_submitted"
        if order.get("checkout_opened_at"):
            return "checkout_opened"
        return "order_created"

    def _is_payment_complete(self, order: dict) -> bool:
        return bool(int(order.get("webhook_verified") or 0)) and str(order.get("webhook_event_type") or "") == "payment.captured"

    def _is_premium_complete(self, order: dict) -> bool:
        return bool(order.get("premium_activated_at")) or (
            self._is_payment_complete(order)
            and str(order.get("status") or "") in {"paid", "captured"}
            and not self._has_premium_failure(order)
        )

    def _has_premium_failure(self, order: dict) -> bool:
        return self._is_payment_complete(order) and str(order.get("status") or "") == "paid_activation_failed"

    def _is_payment_failed(self, order: dict) -> bool:
        return str(order.get("status") or "") in {"failed", "cancelled", "callback_signature_failed"} or str(
            order.get("payment_status") or ""
        ) == "failed"

    def order_tracker(self, order_id: str) -> dict | None:
        order = self.get_order(order_id)
        if not order:
            return None
        steps = self._tracker_steps(order)
        has_pending = any(step["status"] == "pending" for step in steps)
        has_failure = any(step["status"] == "failed" for step in steps)
        return {
            "order": order,
            "steps": steps,
            "has_pending": has_pending,
            "has_failure": has_failure,
            "current_step": self.current_step(order),
            "status_kind": "failure" if has_failure else ("pending" if has_pending else "success"),
        }

    def _update_order_debug(
        self,
        order_id: str,
        *,
        status: str | object = _UNSET,
        payment_id: str | object = _UNSET,
        callback_verified: int | object = _UNSET,
        webhook_verified: int | object = _UNSET,
        callback_status: str | object = _UNSET,
        webhook_status: str | object = _UNSET,
        failure_reason: str | None | object = _UNSET,
        last_error: str | None | object = _UNSET,
        raw_callback_data: str | None | object = _UNSET,
        webhook_event_type: str | None | object = _UNSET,
        error_reason: str | None | object = _UNSET,
        payment_status: str | None | object = _UNSET,
        premium_result: str | None | object = _UNSET,
        checkout_opened_at: str | object = _UNSET,
        payment_submitted_at: str | object = _UNSET,
        callback_received_at: str | object = _UNSET,
        webhook_received_at: str | object = _UNSET,
        premium_activated_at: str | object = _UNSET,
    ) -> dict | None:
        assignments: list[str] = []
        params: list[object] = []
        for column_name, value in (
            ("status", status),
            ("payment_id", payment_id),
            ("callback_verified", callback_verified),
            ("webhook_verified", webhook_verified),
            ("callback_status", callback_status),
            ("webhook_status", webhook_status),
            ("failure_reason", failure_reason),
            ("last_error", last_error),
            ("raw_callback_data", raw_callback_data),
            ("webhook_event_type", webhook_event_type),
            ("error_reason", error_reason),
            ("payment_status", payment_status),
            ("premium_result", premium_result),
            ("checkout_opened_at", checkout_opened_at),
            ("payment_submitted_at", payment_submitted_at),
            ("callback_received_at", callback_received_at),
            ("webhook_received_at", webhook_received_at),
            ("premium_activated_at", premium_activated_at),
        ):
            if value is _UNSET:
                continue
            assignments.append(f"{column_name} = ?")
            params.append(value)
        assignments.append("updated_at = ?")
        params.append(now_iso())
        params.append(order_id)
        with database.connection() as conn:
            conn.execute(
                f"UPDATE payment_orders SET {', '.join(assignments)} WHERE order_id = ?",
                tuple(params),
            )
            row = conn.execute("SELECT * FROM payment_orders WHERE order_id = ?", (order_id,)).fetchone()
        return dict(row) if row else None

    def mark_checkout_opened(self, order_id: str) -> dict | None:
        return self._update_order_debug(
            order_id,
            checkout_opened_at=now_iso(),
            error_reason=None,
            failure_reason=None,
            last_error=None,
        )

    def mark_pay_button_clicked(self, order_id: str) -> dict | None:
        return self._update_order_debug(order_id, error_reason=None, failure_reason=None, last_error=None)

    def mark_callback_received(
        self,
        order_id: str,
        *,
        payment_id: str | None,
        verified: bool,
        error_reason: str | None = None,
        raw_callback_data: str | None = None,
        status: str | None = None,
        submitted: bool = True,
    ) -> dict | None:
        return self._update_order_debug(
            order_id,
            payment_id=payment_id,
            callback_verified=1 if verified else 0,
            callback_status="verified" if verified else "failed",
            failure_reason=error_reason,
            last_error=error_reason,
            raw_callback_data=raw_callback_data,
            error_reason=error_reason,
            callback_received_at=now_iso(),
            payment_submitted_at=now_iso() if submitted else _UNSET,
            payment_status="captured" if verified else "failed",
            status=status if status is not None else _UNSET,
        )

    def mark_webhook_received(
        self,
        order_id: str,
        *,
        payment_id: str | None,
        verified: bool,
        event_type: str | None = None,
        error_reason: str | None = None,
    ) -> dict | None:
        return self._update_order_debug(
            order_id,
            payment_id=payment_id,
            webhook_verified=1 if verified else 0,
            webhook_status="verified" if verified else "failed",
            failure_reason=error_reason,
            last_error=error_reason,
            webhook_event_type=event_type,
            error_reason=error_reason,
            payment_status="captured" if verified else _UNSET,
            webhook_received_at=now_iso(),
        )

    def mark_premium_completed(self, order_id: str, *, result: str) -> dict | None:
        return self._update_order_debug(
            order_id,
            status="paid",
            payment_status="captured",
            premium_result=result,
            premium_activated_at=now_iso(),
            error_reason=None,
            failure_reason=None,
            last_error=None,
        )

    def mark_premium_activated(self, order_id: str) -> dict | None:
        return self.mark_premium_completed(order_id, result="activated")

    def mark_payment_failed(
        self,
        order_id: str,
        *,
        payment_id: str | None,
        reason: str,
        raw_callback_data: str | None = None,
        callback_status: str | None = "failed",
        webhook_status: str | None = None,
        event_type: str | None = None,
    ) -> dict | None:
        logger.warning("payment_failure_reason_saved | order_id=%s payment_id=%s reason=%s", order_id, payment_id, reason)
        return self._update_order_debug(
            order_id,
            status="failed",
            payment_id=payment_id,
            payment_status="failed",
            callback_verified=0 if callback_status else _UNSET,
            callback_status=callback_status if callback_status is not None else _UNSET,
            webhook_status=webhook_status if webhook_status is not None else _UNSET,
            webhook_event_type=event_type if event_type is not None else _UNSET,
            failure_reason=reason,
            last_error=reason,
            error_reason=reason,
            raw_callback_data=raw_callback_data if raw_callback_data is not None else _UNSET,
            callback_received_at=now_iso() if callback_status is not None else _UNSET,
            webhook_received_at=now_iso() if webhook_status is not None else _UNSET,
        )

    def _normalize_price_plan_type(self, plan_type: str) -> str | None:
        return PREMIUM_PRICE_PLAN_ALIASES.get((plan_type or "").strip().lower())

    def _price_setting_key(self, plan_type: str) -> str:
        normalized = self._normalize_price_plan_type(plan_type)
        if not normalized:
            raise ValueError("Invalid premium plan")
        return f"premium_price:{normalized}"

    def _get_setting_value(self, key: str) -> str | None:
        with database.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _set_setting_value(self, key: str, value: str) -> None:
        with database.connection() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now_iso()),
            )

    def get_plan(self, plan_type: str) -> dict:
        if plan_type not in SUBSCRIPTION_PLANS:
            raise ValueError("Invalid plan selected")

        plan = dict(SUBSCRIPTION_PLANS[plan_type])
        if plan_type in {"week_1", "month_1", "months_3"}:
            stored_value = self._get_setting_value(self._price_setting_key(plan_type))
            if stored_value is not None:
                plan["amount"] = int(stored_value)
                logger.info(
                    "premium_price_loaded_from_settings | plan_type=%s amount_paise=%s amount_rupees=%s",
                    plan_type,
                    plan["amount"],
                    f"{Decimal(int(plan['amount'])) / Decimal('100'):.2f}",
                )
        return plan

    def list_checkout_plans(self) -> list[dict]:
        plans = []
        for plan_type in ("week_1", "month_1", "months_3", "year_1"):
            plan = self.get_plan(plan_type)
            plans.append(
                {
                    "plan_type": plan_type,
                    "name": plan["name"],
                    "days": plan["days"],
                    "amount": int(plan["amount"]),
                    "amount_rupees": Decimal(int(plan["amount"])) / Decimal("100"),
                }
            )
        return plans

    def list_premium_prices(self) -> list[dict]:
        items = []
        for display_key, internal_key in (("week_1", "week_1"), ("month_1", "month_1"), ("month_3", "months_3")):
            plan = self.get_plan(internal_key)
            items.append(
                {
                    "key": display_key,
                    "plan_type": internal_key,
                    "name": plan["name"],
                    "amount_paise": int(plan["amount"]),
                    "amount_rupees": Decimal(int(plan["amount"])) / Decimal("100"),
                }
            )
        return items

    def update_premium_price(self, plan_type: str, amount_text: str) -> dict:
        normalized_plan_type = self._normalize_price_plan_type(plan_type)
        if normalized_plan_type not in {"week_1", "month_1", "months_3"}:
            raise ValueError("Invalid premium plan. Use week_1, month_1, or month_3.")

        try:
            amount_rupees = Decimal((amount_text or "").strip())
        except InvalidOperation as exc:
            raise ValueError("Amount must be numeric.") from exc

        if amount_rupees < Decimal("1"):
            raise ValueError("Amount must be at least 1.")

        amount_paise = int((amount_rupees * Decimal("100")).quantize(Decimal("1")))
        self._set_setting_value(self._price_setting_key(normalized_plan_type), str(amount_paise))
        plan = self.get_plan(normalized_plan_type)
        logger.info(
            "premium_price_updated | plan_type=%s amount_paise=%s amount_rupees=%s",
            normalized_plan_type,
            amount_paise,
            f"{Decimal(amount_paise) / Decimal('100'):.2f}",
        )
        return {
            "display_plan_type": "month_3" if normalized_plan_type == "months_3" else normalized_plan_type,
            "plan_type": normalized_plan_type,
            "name": plan["name"],
            "amount_paise": amount_paise,
            "amount_rupees": Decimal(amount_paise) / Decimal("100"),
        }

    def _compute_premium_expiry(self, user: dict, plan_type: str) -> str:
        plan = self.get_plan(plan_type)
        now = datetime.now(timezone.utc)
        current_expiry = user.get("premium_expires_at")
        if current_expiry:
            try:
                current_dt = datetime.fromisoformat(current_expiry)
                if current_dt.tzinfo is None:
                    current_dt = current_dt.replace(tzinfo=timezone.utc)
                start = current_dt if current_dt > now else now
            except ValueError:
                start = now
        else:
            start = now
        return (start + timedelta(days=plan["days"])).replace(microsecond=0).isoformat()

    def ensure_premium_active_for_order(self, order_id: str, *, source: str | None = None) -> dict:
        logger.info(
            "premium_activation_attempt | order_id=%s source=%s",
            order_id,
            source,
        )
        logger.info("premium_activation_started | order_id=%s source=%s", order_id, source)
        logger.info("premium activation start | order_id=%s source=%s", order_id, source)
        if source != PREMIUM_ACTIVATION_SOURCE_WEBHOOK:
            logger.warning(
                "premium_activation_blocked_non_webhook | order_id=%s source=%s",
                order_id,
                source,
            )
            logger.warning("premium_activation_failed | order_id=%s source=%s reason=non_webhook_source", order_id, source)
            logger.info(
                "premium activation end | order_id=%s ok=%s reason=%s",
                order_id,
                False,
                "non_webhook_source",
            )
            return {"ok": False, "reason": "non_webhook_source", "activated_now": False}
        order = self.get_order(order_id)
        if not order:
            logger.warning("premium_activation_failed | order_id=%s source=%s reason=order_not_found", order_id, source)
            logger.info("premium activation end | order_id=%s ok=%s reason=%s", order_id, False, "order_not_found")
            return {"ok": False, "reason": "order_not_found"}
        result = self.ensure_premium_active_for_order_data(order, source=source)
        if not result.get("ok"):
            logger.warning("premium_activation_failed | order_id=%s source=%s reason=%s", order_id, source, result.get("reason"))
        logger.info(
            "premium activation end | order_id=%s ok=%s reason=%s activated_now=%s",
            order_id,
            result.get("ok"),
            result.get("reason"),
            result.get("activated_now"),
        )
        return result

    def ensure_premium_active_for_order_data(self, order_data: dict, *, source: str | None = None) -> dict:
        if source != PREMIUM_ACTIVATION_SOURCE_WEBHOOK:
            logger.warning(
                "premium_activation_blocked_non_webhook | order_id=%s source=%s",
                order_data.get("order_id"),
                source,
            )
            return {"ok": False, "reason": "non_webhook_source", "activated_now": False}

        plan_type = order_data.get("plan_type")
        if plan_type not in SUBSCRIPTION_PLANS:
            return {"ok": True, "reason": "non_premium_plan", "activated_now": False}

        user_id = order_data["user_id"]
        user = user_service.get_user(user_id)
        if not user:
            logger.warning(
                "Premium activation skipped | order_id=%s user_id=%s reason=user_not_found",
                order_data.get("order_id"),
                user_id,
            )
            return {"ok": False, "reason": "user_not_found"}

        premium_active = bool(user.get("is_premium")) and bool(user.get("premium_expires_at"))
        was_active_and_unexpired = False
        if premium_active:
            expiry_dt = parse_utc_datetime(user["premium_expires_at"])
            was_active_and_unexpired = bool(expiry_dt and expiry_dt > datetime.now(timezone.utc))
        expiry = self._compute_premium_expiry(user, plan_type)
        user_service.set_premium_expiry(user_id, expiry, True)
        premium_result = "extended" if was_active_and_unexpired else "activated"
        self.mark_premium_completed(order_data["order_id"], result=premium_result)
        logger.info(
            "premium_activation_success | order_id=%s user_id=%s plan_type=%s final_order_status=%s expiry=%s result=%s",
            order_data.get("order_id"),
            user_id,
            plan_type,
            order_data.get("status"),
            expiry,
            premium_result,
        )
        logger.info("premium_activated | order_id=%s user_id=%s plan_type=%s result=%s", order_data.get("order_id"), user_id, plan_type, premium_result)
        return {
            "ok": True,
            "reason": premium_result,
            "activated_now": not was_active_and_unexpired,
            "extended_now": was_active_and_unexpired,
            "expiry": expiry,
            "user_id": user_id,
            "plan_type": plan_type,
        }

    def _save_order_record(
        self,
        *,
        order_id: str,
        user_id: int,
        plan_type: str,
        amount: int,
        currency: str,
        status: str,
        payment_url: str,
    ):
        created_at = now_iso()
        with database.connection() as conn:
            conn.execute(
                """
                INSERT INTO payment_orders (
                    order_id, user_id, plan_type, amount, currency, status, payment_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    plan_type = excluded.plan_type,
                    amount = excluded.amount,
                    currency = excluded.currency,
                    status = excluded.status,
                    payment_url = excluded.payment_url,
                    updated_at = excluded.updated_at
                """,
                (
                    order_id,
                    user_id,
                    plan_type,
                    amount,
                    currency,
                    status,
                    payment_url,
                    created_at,
                    created_at,
                ),
            )
            saved_row = conn.execute(
                """
                SELECT order_id, user_id, plan_type, amount, currency, status, payment_url, created_at, updated_at
                FROM payment_orders
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchone()
        logger.info(
            "Payment order saved | order_id=%s row_exists=%s user_id=%s plan_type=%s amount=%s currency=%s status=%s",
            order_id,
            bool(saved_row),
            user_id,
            plan_type,
            amount,
            currency,
            status,
        )

    def get_missing_configuration(self) -> list[str]:
        missing = []

        missing.extend(self.get_checkout_blockers())
        if not (RAZORPAY_WEBHOOK_SECRET or "").strip():
            missing.append("PAYMENT_WEBHOOK_SECRET")
        if not (os.getenv("PUBLIC_BASE_URL", "") or "").strip():
            missing.append("PUBLIC_BASE_URL")
        if not (BOT_USERNAME or "").strip() or (BOT_USERNAME or "").strip() == "YOUR_BOT_USERNAME":
            missing.append("BOT_USERNAME")

        return missing

    def set_order_status_if_not_paid(self, order_id: str, status: str) -> tuple[dict | None, bool]:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM payment_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if not row:
                return None, False

            order = dict(row)
            if order.get("status") == "paid":
                return order, False

            conn.execute(
                "UPDATE payment_orders SET status = ?, updated_at = ? WHERE order_id = ?",
                (status, now_iso(), order_id),
            )
            updated_row = conn.execute(
                "SELECT * FROM payment_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return dict(updated_row) if updated_row else order, True

    async def create_order(self, user_id: int, plan_type: str) -> dict:
        if plan_type not in SUBSCRIPTION_PLANS:
            raise ValueError("Invalid plan selected")

        checkout_blockers = self.get_checkout_blockers()
        if checkout_blockers:
            raise ValueError(f"Checkout unavailable: {'; '.join(checkout_blockers)}")

        plan = self.get_plan(plan_type)
        logger.info("order_create_started | source=bot user_id=%s plan_type=%s", user_id, plan_type)
        logger.info(
            "premium_price_used_for_order | user_id=%s plan_type=%s amount_paise=%s amount_rupees=%s",
            user_id,
            plan_type,
            plan["amount"],
            f"{Decimal(int(plan['amount'])) / Decimal('100'):.2f}",
        )
        payload = {
            "amount": plan["amount"],
            "currency": "INR",
            "receipt": f"quizbot_{user_id}_{int(datetime.utcnow().timestamp())}",
            "notes": {
                "user_id": str(user_id),
                "plan_type": plan_type,
            },
        }

        async with httpx.AsyncClient(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=20) as client:
            response = await client.post("https://api.razorpay.com/v1/orders", json=payload)
            response.raise_for_status()
            order = response.json()

        payment_url = f"{(PUBLIC_BASE_URL or '').rstrip('/')}/payment/{order['id']}" if PUBLIC_BASE_URL else f"/payment/{order['id']}"
        self._save_order_record(
            order_id=order["id"],
            user_id=user_id,
            plan_type=plan_type,
            amount=plan["amount"],
            currency=order.get("currency", "INR"),
            status=order.get("status", "created"),
            payment_url=payment_url,
        )
        logger.info("order_create_success | source=bot user_id=%s plan_type=%s order_id=%s", user_id, plan_type, order["id"])

        return {
            "order_id": order["id"],
            "payment_url": payment_url,
            "plan_name": plan["name"],
            "amount": plan["amount"],
            "currency": "INR",
        }

    async def create_test_order(self, user_id: int) -> dict:
        checkout_blockers = self.get_checkout_blockers()
        if checkout_blockers:
            raise ValueError(f"Checkout unavailable: {'; '.join(checkout_blockers)}")

        logger.info("order_create_started | source=admin_test user_id=%s plan_type=test_order", user_id)
        payload = {
            "amount": 100,
            "currency": "INR",
            "receipt": f"quizbot_test_{user_id}_{int(datetime.utcnow().timestamp())}",
            "notes": {
                "user_id": str(user_id),
                "purpose": "admin_test_order",
            },
        }

        async with httpx.AsyncClient(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=20) as client:
            response = await client.post("https://api.razorpay.com/v1/orders", json=payload)
            response.raise_for_status()
            order = response.json()

        payment_url = f"{(PUBLIC_BASE_URL or '').rstrip('/')}/payment/{order['id']}" if PUBLIC_BASE_URL else f"/payment/{order['id']}"
        self._save_order_record(
            order_id=order["id"],
            user_id=user_id,
            plan_type="test_order",
            amount=payload["amount"],
            currency=order.get("currency", "INR"),
            status=order.get("status", "created"),
            payment_url=payment_url,
        )
        logger.info("order_create_success | source=admin_test user_id=%s plan_type=test_order order_id=%s", user_id, order["id"])

        return {
            "order_id": order["id"],
            "payment_url": payment_url,
            "plan_name": "Test Payment Order",
            "amount": payload["amount"],
            "currency": payload["currency"],
        }

    def get_order(self, order_id: str) -> dict | None:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM payment_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if not row:
            return None
        return self.normalize_order_state(dict(row))

    def _reconcile_verified_captured_order(self, order: dict) -> dict | None:
        if not self._is_payment_complete(order):
            return None
        if self._has_premium_failure(order):
            return None

        updates: dict[str, object] = {}
        if str(order.get("status") or "") not in {"paid", "captured"}:
            updates["status"] = "paid"
        if str(order.get("payment_status") or "") != "captured":
            updates["payment_status"] = "captured"
        if not order.get("premium_activated_at"):
            updates["premium_activated_at"] = order.get("webhook_received_at") or now_iso()
        if not order.get("premium_result"):
            updates["premium_result"] = "activated"
        if not updates:
            return None
        return self._update_order_debug(order["order_id"], **updates)

    def normalize_order_state(self, order: dict | None) -> dict | None:
        if not order:
            return None
        reconciled = self._reconcile_verified_captured_order(dict(order))
        return reconciled or dict(order)

    async def get_order_with_fallback(self, order_id: str) -> tuple[dict | None, str]:
        order = self.get_order(order_id)
        if order:
            return order, "database"

        remote_order = await self.fetch_razorpay_order(order_id)
        if not remote_order:
            return None, "missing"

        notes = remote_order.get("notes") or {}
        user_id_raw = notes.get("user_id")
        plan_type = notes.get("plan_type") or notes.get("purpose") or "unknown"
        if not str(user_id_raw or "").isdigit():
            logger.warning(
                "Remote order cannot be restored locally | order_id=%s reason=missing_user_id notes=%s",
                order_id,
                notes,
            )
            return None, "razorpay_missing_user_id"

        payment_url = f"{(PUBLIC_BASE_URL or '').rstrip('/')}/payment/{remote_order['id']}" if PUBLIC_BASE_URL else f"/payment/{remote_order['id']}"
        self._save_order_record(
            order_id=remote_order["id"],
            user_id=int(user_id_raw),
            plan_type=plan_type,
            amount=int(remote_order.get("amount") or 0),
            currency=remote_order.get("currency", "INR"),
            status=remote_order.get("status", "created"),
            payment_url=payment_url,
        )
        restored_order = self.get_order(order_id)
        if restored_order:
            logger.info(
                "Payment order restored from Razorpay | order_id=%s user_id=%s plan_type=%s",
                order_id,
                user_id_raw,
                plan_type,
            )
            return restored_order, "razorpay_restored"

        return None, "razorpay_restore_failed"

    def get_order_with_fallback_sync(self, order_id: str) -> tuple[dict | None, str]:
        order = self.get_order(order_id)
        if order:
            return order, "database"

        remote_order = self.fetch_razorpay_order_sync(order_id)
        if not remote_order:
            return None, "missing"

        notes = remote_order.get("notes") or {}
        user_id_raw = notes.get("user_id")
        plan_type = notes.get("plan_type") or notes.get("purpose") or "unknown"
        if not str(user_id_raw or "").isdigit():
            logger.warning(
                "Remote order cannot be restored locally | order_id=%s reason=missing_user_id notes=%s",
                order_id,
                notes,
            )
            return None, "razorpay_missing_user_id"

        payment_url = f"{(PUBLIC_BASE_URL or '').rstrip('/')}/payment/{remote_order['id']}" if PUBLIC_BASE_URL else f"/payment/{remote_order['id']}"
        self._save_order_record(
            order_id=remote_order["id"],
            user_id=int(user_id_raw),
            plan_type=plan_type,
            amount=int(remote_order.get("amount") or 0),
            currency=remote_order.get("currency", "INR"),
            status=remote_order.get("status", "created"),
            payment_url=payment_url,
        )
        restored_order = self.get_order(order_id)
        if restored_order:
            logger.info(
                "Payment order restored from Razorpay | order_id=%s user_id=%s plan_type=%s",
                order_id,
                user_id_raw,
                plan_type,
            )
            return restored_order, "razorpay_restored"

        return None, "razorpay_restore_failed"

    def update_order_status(self, order_id: str, status: str):
        with database.connection() as conn:
            conn.execute(
                "UPDATE payment_orders SET status = ?, updated_at = ? WHERE order_id = ?",
                (status, now_iso(), order_id),
            )

    async def fetch_razorpay_payment(self, payment_id: str) -> dict | None:
        if not payment_id or not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
            return None

        async with httpx.AsyncClient(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=20) as client:
            response = await client.get(f"https://api.razorpay.com/v1/payments/{payment_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    def fetch_razorpay_payment_sync(self, payment_id: str) -> dict | None:
        if not payment_id or not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
            return None

        with httpx.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=20) as client:
            response = client.get(f"https://api.razorpay.com/v1/payments/{payment_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    async def fetch_razorpay_order(self, order_id: str) -> dict | None:
        if not order_id or not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
            return None

        async with httpx.AsyncClient(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=20) as client:
            response = await client.get(f"https://api.razorpay.com/v1/orders/{order_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    def fetch_razorpay_order_sync(self, order_id: str) -> dict | None:
        if not order_id or not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
            return None

        with httpx.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=20) as client:
            response = client.get(f"https://api.razorpay.com/v1/orders/{order_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        if not RAZORPAY_WEBHOOK_SECRET:
            return False

        expected = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_payment_signature(
        self,
        *,
        order_id: str,
        payment_id: str,
        signature: str,
    ) -> bool:
        if not RAZORPAY_KEY_SECRET:
            return False

        expected = hmac.new(
            RAZORPAY_KEY_SECRET.encode("utf-8"),
            f"{order_id}|{payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def process_captured_payment(self, event_id: str, payload: dict):
        event_name = payload.get("event")
        logger.info("webhook_event_type | event_id=%s event=%s", event_id, event_name)
        if event_name != "payment.captured":
            logger.info("Ignoring webhook event | event_id=%s event=%s", event_id, event_name)
            return {"status": "ignored", "reason": "unsupported_event"}

        payment_entity = (
            payload.get("payload", {})
            .get("payment", {})
            .get("entity", {})
        )

        payment_id = payment_entity.get("id")
        order_id = payment_entity.get("order_id")
        amount = payment_entity.get("amount")
        currency = payment_entity.get("currency", "INR")
        payment_status = payment_entity.get("status")
        logger.info(
            "webhook_received | event_id=%s event=%s order_id=%s payment_id=%s",
            event_id,
            event_name,
            order_id,
            payment_id,
        )

        if not payment_id or not order_id or amount is None:
            raise ValueError("Missing payment fields in webhook payload")
        if payment_status != "captured":
            raise ValueError("Webhook payment status is not captured")

        with database.connection() as conn:
            duplicate_status = self._get_duplicate_status(conn, event_id=event_id, payment_id=payment_id, order_id=order_id)
            order = conn.execute(
                "SELECT * FROM payment_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if not order:
                raise ValueError("Order not found for captured payment")

            order_data = dict(order)
            if duplicate_status["duplicate"]:
                self._record_duplicate_attempt(
                    conn,
                    event_id=event_id,
                    payment_id=payment_id,
                    order_id=order_id,
                    existing_event=duplicate_status["existing_event"],
                )
                logger.info(
                    "Duplicate webhook already processed | event_id=%s order_id=%s payment_id=%s reason=%s duplicate_count=%s",
                    event_id,
                    order_id,
                    payment_id,
                    duplicate_status["reason"],
                    duplicate_status["duplicate_count"],
                )
                return {
                    "status": "already_processed",
                    "reason": duplicate_status["reason"],
                    "user_id": order_data["user_id"],
                    "plan_type": order_data["plan_type"],
                }

            expected_amount = order_data["amount"]
            if amount != expected_amount:
                logger.warning(
                    "Webhook amount mismatch | event_id=%s order_id=%s expected_amount=%s received_amount=%s",
                    event_id,
                    order_id,
                    expected_amount,
                    amount,
                )
                self.mark_webhook_received(
                    order_id,
                    payment_id=payment_id,
                    verified=False,
                    event_type=event_name,
                    error_reason="amount_mismatch",
                )
                raise ValueError("Payment amount does not match the saved order")

            plan_type = order_data["plan_type"]
            expiry = now_iso()
            should_activate_premium = plan_type in SUBSCRIPTION_PLANS

            if should_activate_premium:
                user = user_service.get_user(order_data["user_id"])
                if not user:
                    logger.warning(
                        "Webhook user not found | event_id=%s order_id=%s user_id=%s",
                        event_id,
                        order_id,
                        order_data["user_id"],
                    )
                    raise ValueError("User not found for captured payment")
                expiry = self._compute_premium_expiry(user, plan_type)

            conn.execute(
                """
                INSERT INTO processed_webhooks (
                    event_id, payment_id, order_id, received_at, last_seen_at, duplicate_count
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (event_id, payment_id, order_id, now_iso(), now_iso()),
            )
            conn.execute(
                """
                INSERT INTO payments (
                    payment_id, order_id, user_id, plan_type, amount, currency, status,
                    timestamp, expiry_date, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(payment_id) DO UPDATE SET
                    order_id = excluded.order_id,
                    user_id = excluded.user_id,
                    plan_type = excluded.plan_type,
                    amount = excluded.amount,
                    currency = excluded.currency,
                    status = excluded.status,
                    timestamp = excluded.timestamp,
                    expiry_date = excluded.expiry_date,
                    raw_payload = excluded.raw_payload
                """,
                (
                    payment_id,
                    order_id,
                    order_data["user_id"],
                    plan_type,
                    amount,
                    currency,
                    payment_status,
                    now_iso(),
                    expiry,
                    json.dumps(payload),
                ),
            )
            conn.execute(
                "UPDATE payment_orders SET status = ?, payment_status = ?, updated_at = ? WHERE order_id = ?",
                ("paid", "captured", now_iso(), order_id),
            )
        self.mark_webhook_received(
            order_id,
            payment_id=payment_id,
            verified=True,
            event_type=event_name,
            error_reason=None,
        )

        activation_result = self.ensure_premium_active_for_order(
            order_id,
            source=PREMIUM_ACTIVATION_SOURCE_WEBHOOK,
        )
        if should_activate_premium and not activation_result.get("ok"):
            self._update_order_debug(
                order_id,
                status="paid_activation_failed",
                payment_status="captured",
                failure_reason=str(activation_result.get("reason") or "premium_activation_failed"),
                last_error=str(activation_result.get("reason") or "premium_activation_failed"),
                error_reason=str(activation_result.get("reason") or "premium_activation_failed"),
            )
        if not should_activate_premium:
            logger.info(
                "Webhook payment recorded without premium activation | event_id=%s order_id=%s plan_type=%s",
                event_id,
                order_id,
                plan_type,
            )

        return {
            "status": "processed",
            "user_id": order_data["user_id"],
            "plan_type": plan_type,
            "expiry": expiry,
            "activation_result": activation_result,
        }

    def process_failed_payment(self, event_id: str, payload: dict):
        event_name = payload.get("event")
        logger.info("webhook_event_type | event_id=%s event=%s", event_id, event_name)
        if event_name != "payment.failed":
            logger.info("Ignoring webhook event | event_id=%s event=%s", event_id, event_name)
            return {"status": "ignored", "reason": "unsupported_event"}

        payment_entity = (
            payload.get("payload", {})
            .get("payment", {})
            .get("entity", {})
        )
        payment_id = payment_entity.get("id")
        order_id = payment_entity.get("order_id")
        payment_status = payment_entity.get("status") or "failed"
        error_description = (
            payment_entity.get("error_description")
            or payment_entity.get("error_reason")
            or payment_entity.get("error_code")
            or payload.get("error", {}).get("description")
            or "payment_failed"
        )
        logger.warning(
            "payment_failed_webhook_received | event_id=%s order_id=%s payment_id=%s reason=%s",
            event_id,
            order_id,
            payment_id,
            error_description,
        )

        if not payment_id or not order_id:
            raise ValueError("Missing payment fields in failed webhook payload")

        with database.connection() as conn:
            duplicate_status = self._get_duplicate_status(conn, event_id=event_id, payment_id=payment_id, order_id=order_id)
            order = conn.execute(
                "SELECT * FROM payment_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if not order:
                raise ValueError("Order not found for failed payment")

            order_data = dict(order)
            if duplicate_status["duplicate"]:
                self._record_duplicate_attempt(
                    conn,
                    event_id=event_id,
                    payment_id=payment_id,
                    order_id=order_id,
                    existing_event=duplicate_status["existing_event"],
                )
                logger.info(
                    "Duplicate failed-payment webhook already processed | event_id=%s order_id=%s payment_id=%s reason=%s duplicate_count=%s",
                    event_id,
                    order_id,
                    payment_id,
                    duplicate_status["reason"],
                    duplicate_status["duplicate_count"],
                )
                return {
                    "status": "already_processed",
                    "reason": duplicate_status["reason"],
                    "user_id": order_data["user_id"],
                    "plan_type": order_data["plan_type"],
                }

            conn.execute(
                """
                INSERT INTO processed_webhooks (
                    event_id, payment_id, order_id, received_at, last_seen_at, duplicate_count
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (event_id, payment_id, order_id, now_iso(), now_iso()),
            )

        self.mark_payment_failed(
            order_id,
            payment_id=payment_id,
            reason=str(error_description),
            raw_callback_data=json.dumps(payload),
            callback_status=None,
            webhook_status="verified",
            event_type=event_name,
        )
        return {
            "status": "failed",
            "reason": str(error_description),
            "payment_status": payment_status,
            "user_id": order_data["user_id"],
            "plan_type": order_data["plan_type"],
        }

    def process_payment_webhook(self, event_id: str, payload: dict):
        event_name = payload.get("event")
        if event_name == "payment.captured":
            return self.process_captured_payment(event_id, payload)
        if event_name == "payment.failed":
            return self.process_failed_payment(event_id, payload)
        logger.info("Ignoring webhook event | event_id=%s event=%s", event_id, event_name)
        return {"status": "ignored", "reason": "unsupported_event"}

    def check_processed_webhook(self, event_id: str, payment_id: str | None, order_id: str | None) -> dict:
        with database.connection() as conn:
            duplicate_status = self._get_duplicate_status(
                conn,
                event_id=event_id,
                payment_id=payment_id,
                order_id=order_id,
            )
            if not duplicate_status["duplicate"]:
                return {"duplicate": False}

            self._record_duplicate_attempt(
                conn,
                event_id=event_id,
                payment_id=payment_id,
                order_id=order_id,
                existing_event=duplicate_status["existing_event"],
            )
            return duplicate_status

    def _get_duplicate_status(self, conn, *, event_id: str, payment_id: str | None, order_id: str | None) -> dict:
        event_row = conn.execute(
            "SELECT event_id, duplicate_count FROM processed_webhooks WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if event_row:
            return {
                "duplicate": True,
                "reason": "duplicate_event",
                "existing_event": event_row["event_id"],
                "duplicate_count": min(int(event_row["duplicate_count"] or 0) + 1, MAX_WEBHOOK_DUPLICATE_COUNT),
            }

        payment_row = None
        if payment_id or order_id:
            where_clauses = []
            params: list[str] = []
            if payment_id:
                where_clauses.append("payment_id = ?")
                params.append(payment_id)
            if order_id:
                where_clauses.append("order_id = ?")
                params.append(order_id)
            payment_row = conn.execute(
                f"""
                SELECT payment_id, order_id
                FROM payments
                WHERE {" OR ".join(where_clauses)}
                """,
                tuple(params),
            ).fetchone()
        if payment_row:
            return {
                "duplicate": True,
                "reason": "payment_exists",
                "existing_event": None,
                "duplicate_count": 1,
            }

        return {"duplicate": False}

    def _record_duplicate_attempt(
        self,
        conn,
        *,
        event_id: str,
        payment_id: str | None,
        order_id: str | None,
        existing_event: str | None,
    ) -> None:
        timestamp = now_iso()
        target_event_id = existing_event or event_id
        current_row = conn.execute(
            "SELECT duplicate_count FROM processed_webhooks WHERE event_id = ?",
            (target_event_id,),
        ).fetchone()
        if current_row:
            next_count = min(int(current_row["duplicate_count"] or 0) + 1, MAX_WEBHOOK_DUPLICATE_COUNT)
            conn.execute(
                """
                UPDATE processed_webhooks
                SET last_seen_at = ?, duplicate_count = ?, payment_id = COALESCE(payment_id, ?), order_id = COALESCE(order_id, ?)
                WHERE event_id = ?
                """,
                (timestamp, next_count, payment_id, order_id, target_event_id),
            )
            return

        conn.execute(
            """
            INSERT INTO processed_webhooks (
                event_id, payment_id, order_id, received_at, last_seen_at, duplicate_count
            ) VALUES (?, ?, ?, ?, ?, 1)
            """,
            (event_id, payment_id, order_id, timestamp, timestamp),
        )

    def premium_status_text(self, user: dict) -> str:
        expiry = parse_utc_datetime(user.get("premium_expires_at"))
        if expiry and expiry > datetime.now(timezone.utc):
            return f"Plan: Premium | Expiry: {expiry.replace(microsecond=0).isoformat()} UTC"
        return "Plan: Premium" if user.get("is_premium") and not user.get("premium_expires_at") else "Plan: Free"


payment_service = PaymentService()
