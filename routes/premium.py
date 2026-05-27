import hashlib
import json

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from config import BOT_URL, PUBLIC_BASE_URL, RAZORPAY_KEY_ID
from routes.auth import login_required
from services.payment_service_db import payment_service
from services.site_content import PREMIUM_BENEFITS
from services.web_identity_service import web_identity_service
from services.web_payment_service import web_payment_service
from utils.logging_utils import get_logger


premium_blueprint = Blueprint("premium", __name__)
logger = get_logger(__name__)


def _render_payment_tracker(
    *,
    order_id: str | None,
    page_title: str,
    title: str,
    message: str,
    detail: str,
    verified: bool,
):
    tracker = payment_service.order_tracker(order_id) if order_id else None
    order = tracker["order"] if tracker else None
    return render_template(
        "payment_status.html",
        page_title=page_title,
        status_kind=(tracker or {}).get("status_kind", "pending" if verified else "failure"),
        title=title,
        message=message,
        detail=detail,
        order=order,
        verified=verified,
        tracker_steps=(tracker or {}).get("steps", []),
        current_step=(tracker or {}).get("current_step"),
        auto_refresh=bool(tracker and tracker.get("has_pending") and not tracker.get("has_failure")),
        status_url=url_for("premium.payment_status_page", order_id=order_id) if order_id else None,
        bot_url=BOT_URL,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@premium_blueprint.route("/premium", methods=["GET", "POST"])
@login_required
def premium_page():
    user = web_identity_service.refresh_authenticated_user()
    checkout_blockers = payment_service.get_checkout_blockers()
    if request.method == "POST":
        plan_type = request.form.get("plan_type", "")
        try:
            order = web_payment_service.create_order(user["user_id"], plan_type)
        except Exception as exc:
            logger.warning(
                "premium_checkout_create_failed | user_id=%s plan_type=%s reason=%s",
                user.get("user_id"),
                plan_type,
                exc,
            )
            flash(str(exc), "error")
            return redirect(url_for("premium.premium_page"))
        return redirect(url_for("premium.payment_page", order_id=order["order_id"]))

    if checkout_blockers:
        logger.info(
            "premium_checkout_disabled | user_id=%s reasons=%s",
            user.get("user_id"),
            "; ".join(checkout_blockers),
        )

    return render_template(
        "premium.html",
        page_title="Premium Plans",
        user=user,
        premium_prices=payment_service.list_premium_prices(),
        payment_ready=payment_service.checkout_ready(),
        payment_mode_name=payment_service.checkout_mode_name(),
        payment_is_test_mode=payment_service.is_test_mode(),
        checkout_blockers=checkout_blockers,
        payment_config_issues=payment_service.get_missing_configuration(),
        payment_mode_note=payment_service.payment_mode_note(),
        premium_status=payment_service.premium_status_text(user),
        premium_benefits=PREMIUM_BENEFITS,
        bot_url=BOT_URL,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@premium_blueprint.route("/payment/<order_id>")
@login_required
def payment_page(order_id: str):
    user = web_identity_service.refresh_authenticated_user()
    order, _source = payment_service.get_order_with_fallback_sync(order_id)
    if not order:
        flash("Invalid payment order.", "error")
        return redirect(url_for("premium.premium_page"))
    if int(order["user_id"]) != int(user["user_id"]) and not web_identity_service.is_admin_authenticated():
        flash("This payment order does not belong to your account.", "error")
        return redirect(url_for("premium.premium_page"))

    logger.info("payment_page_loaded | order_id=%s user_id=%s", order_id, user.get("user_id"))
    plan = payment_service.get_plan(order["plan_type"])
    checkout_options = {
        "key": RAZORPAY_KEY_ID,
        "amount": order["amount"],
        "currency": order["currency"],
        "name": "QuizPathshala Premium",
        "description": plan["name"],
        "order_id": order_id,
        "notes": {
            "user_id": str(order["user_id"]),
            "plan_type": str(order["plan_type"]),
        },
        "theme": {"color": "#c35b2f"},
    }
    return render_template(
        "payment_page.html",
        page_title="Payment Checkout",
        user=user,
        order=order,
        plan=plan,
        checkout_options=json.dumps(checkout_options),
        public_base_url=PUBLIC_BASE_URL.rstrip("/") or request.url_root.rstrip("/"),
        payment_mode_name=payment_service.checkout_mode_name(),
        payment_is_test_mode=payment_service.is_test_mode(),
        payment_mode_note=payment_service.payment_mode_note(),
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@premium_blueprint.route("/payment/<order_id>/client-event", methods=["POST"])
@login_required
def payment_client_event(order_id: str):
    user = web_identity_service.refresh_authenticated_user()
    order = payment_service.get_order(order_id)
    if not order:
        return jsonify({"detail": "Invalid payment order"}), 404
    if int(order["user_id"]) != int(user["user_id"]) and not web_identity_service.is_admin_authenticated():
        return jsonify({"detail": "Forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    event_name = str(payload.get("event") or "").strip().lower()
    payment_id = (payload.get("payment_id") or "").strip() or None
    reason = (payload.get("reason") or "").strip() or None
    raw_callback_data = payload.get("raw_callback_data")
    serialized_callback = json.dumps(raw_callback_data) if raw_callback_data is not None else None

    if event_name == "pay_button_clicked":
        logger.info("pay_button_clicked | order_id=%s user_id=%s", order_id, user.get("user_id"))
        payment_service.mark_pay_button_clicked(order_id)
    elif event_name == "razorpay_checkout_opened":
        logger.info("razorpay_checkout_opened | order_id=%s user_id=%s", order_id, user.get("user_id"))
        payment_service.mark_checkout_opened(order_id)
    elif event_name == "razorpay_checkout_open_failed":
        logger.warning("razorpay_checkout_open_failed | order_id=%s user_id=%s reason=%s", order_id, user.get("user_id"), reason)
        payment_service._update_order_debug(
            order_id,
            failure_reason=reason or "checkout_open_failed",
            last_error=reason or "checkout_open_failed",
            error_reason=reason or "checkout_open_failed",
        )
    elif event_name == "razorpay_failure_callback_received":
        failure_reason = reason or "payment_failed"
        logger.warning("razorpay_payment_failed_callback | order_id=%s payment_id=%s reason=%s", order_id, payment_id, failure_reason)
        payment_service.mark_payment_failed(
            order_id,
            payment_id=payment_id,
            reason=failure_reason,
            raw_callback_data=serialized_callback,
            callback_status="failed",
        )
    elif event_name == "checkout_cancelled":
        logger.warning("payment_failed | order_id=%s payment_id=%s reason=checkout_cancelled", order_id, payment_id)
        payment_service.mark_payment_failed(
            order_id,
            payment_id=payment_id,
            reason="checkout_cancelled",
            callback_status="cancelled",
        )
    else:
        return jsonify({"detail": "Unknown event"}), 400

    return jsonify({"ok": True})


@premium_blueprint.route("/payment/status/<order_id>")
@login_required
def payment_status_page(order_id: str):
    user = web_identity_service.refresh_authenticated_user()
    order = payment_service.get_order(order_id)
    if not order:
        flash("Invalid payment order.", "error")
        return redirect(url_for("premium.premium_page"))
    if int(order["user_id"]) != int(user["user_id"]) and not web_identity_service.is_admin_authenticated():
        flash("This payment order does not belong to your account.", "error")
        return redirect(url_for("premium.premium_page"))
    tracker = payment_service.order_tracker(order_id) or {}
    has_failure = bool(tracker.get("has_failure"))
    has_pending = bool(tracker.get("has_pending"))
    if has_failure:
        title = "Payment status"
        message = "Payment could not be completed."
        detail = order.get("error_reason") or "Please review the failed step below."
    elif has_pending:
        title = "Payment status"
        message = "Payment submitted, waiting for confirmation."
        detail = "The browser callback was received. Premium will activate only after the Razorpay payment.captured webhook is verified on the server."
    else:
        title = "Payment status"
        message = "Premium has been activated."
        detail = "All required verification steps completed successfully."
    return _render_payment_tracker(
        order_id=order_id,
        page_title="Payment Status",
        title=title,
        message=message,
        detail=detail,
        verified=bool(int(order.get("callback_verified") or 0)),
    )


@premium_blueprint.route("/payment/success", methods=["GET", "POST"])
@login_required
def payment_success():
    web_identity_service.refresh_authenticated_user()
    order_id = request.values.get("razorpay_order_id")
    payment_id = request.values.get("razorpay_payment_id")
    signature = request.values.get("razorpay_signature")
    order = payment_service.get_order(order_id) if order_id else None
    verified = False
    if order_id and payment_id and signature:
        logger.info("razorpay_success_callback_received | order_id=%s payment_id=%s", order_id, payment_id)
        raw_callback_data = json.dumps({key: request.values.get(key) for key in request.values.keys()})
        verified = payment_service.verify_payment_signature(
            order_id=order_id,
            payment_id=payment_id,
            signature=signature,
        )
        if verified:
            logger.info("razorpay_signature_verified | order_id=%s payment_id=%s", order_id, payment_id)
            order, _updated = payment_service.set_order_status_if_not_paid(order_id, "callback_verified")
            payment_service.mark_callback_received(
                order_id,
                payment_id=payment_id,
                verified=True,
                error_reason=None,
                raw_callback_data=raw_callback_data,
                status="callback_verified",
            )
        else:
            logger.warning("razorpay_signature_failed | order_id=%s payment_id=%s", order_id, payment_id)
            order, _updated = payment_service.set_order_status_if_not_paid(order_id, "callback_signature_failed")
            payment_service.mark_callback_received(
                order_id,
                payment_id=payment_id,
                verified=False,
                error_reason="callback_signature_failed",
                raw_callback_data=raw_callback_data,
                status="callback_signature_failed",
            )
            logger.warning("payment_failed | order_id=%s payment_id=%s reason=callback_signature_failed", order_id, payment_id)
    return _render_payment_tracker(
        order_id=order_id,
        page_title="Payment Submitted",
        title="Payment Submitted",
        message="Payment submitted, waiting for confirmation.",
        detail="Your payment details were received from Razorpay. Premium will activate only after the server verifies a payment.captured webhook.",
        verified=verified,
    )


@premium_blueprint.route("/payment/cancel")
@login_required
def payment_cancel():
    order_id = request.args.get("razorpay_order_id")
    payment_id = request.args.get("razorpay_payment_id")
    if order_id:
        payment_service.set_order_status_if_not_paid(order_id, "cancelled")
        payment_service._update_order_debug(
            order_id,
            payment_id=payment_id,
            callback_status="cancelled",
            failure_reason="checkout_cancelled",
            last_error="checkout_cancelled",
            error_reason="checkout_cancelled",
        )
        logger.warning("payment_failed | order_id=%s payment_id=%s reason=checkout_cancelled", order_id, payment_id)
    return _render_payment_tracker(
        order_id=order_id,
        page_title="Payment Cancelled",
        title="Payment Cancelled",
        message="The checkout was closed before payment confirmation.",
        detail="No premium access was activated. You can return to the premium page and start a new payment attempt when ready.",
        verified=False,
    )


@premium_blueprint.route("/payment/failed")
@login_required
def payment_failed():
    order_id = request.args.get("razorpay_order_id")
    payment_id = request.args.get("razorpay_payment_id")
    reason = request.args.get("reason") or "Payment verification failed before completion."
    if order_id:
        logger.warning("razorpay_payment_failed_callback | order_id=%s payment_id=%s reason=%s", order_id, payment_id, reason)
        payment_service.mark_payment_failed(
            order_id,
            payment_id=payment_id,
            reason=reason,
            raw_callback_data=json.dumps(dict(request.args)),
            callback_status="failed",
        )
        logger.warning("payment_failed | order_id=%s payment_id=%s reason=%s", order_id, payment_id, reason)
    return _render_payment_tracker(
        order_id=order_id,
        page_title="Payment Failed",
        title="Payment Failed",
        message="The payment could not be completed.",
        detail=reason,
        verified=False,
    )


@premium_blueprint.route("/webhook", methods=["POST"])
@premium_blueprint.route("/webhook/razorpay", methods=["POST"])
@premium_blueprint.route("/payment/webhook", methods=["POST"])
@premium_blueprint.route("/payment/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("X-Razorpay-Event-Id")
    logger.info("webhook_received | event_id=%s content_length=%s", event_id, len(raw_body or b""))

    if not payment_service.verify_webhook_signature(raw_body, signature):
        logger.warning("webhook_signature_failed | event_id=%s", event_id)
        return jsonify({"detail": "Invalid webhook signature"}), 401
    logger.info("webhook_signature_verified | event_id=%s", event_id)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        return jsonify({"detail": "Invalid webhook payload"}), 400

    derived_event_id = (
        event_id
        or payload.get("payload", {}).get("payment", {}).get("entity", {}).get("id")
        or hashlib.sha256(raw_body).hexdigest()
    )
    result = payment_service.process_payment_webhook(derived_event_id, payload)
    return jsonify(result)
