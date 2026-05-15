from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from config import SUPER_ADMIN_EMAIL, SUPPORT_TELEGRAM
from services.web_identity_service import web_identity_service
from services.web_password_reset_service import web_password_reset_service
from services.web_quiz_service import web_quiz_service
from utils.logging_utils import get_logger


auth_blueprint = Blueprint("auth", __name__)
RESET_REQUEST_ID_SESSION_KEY = "password_reset_request_id"
RESET_TOKEN_SESSION_KEY = "password_reset_token"
RESET_USER_ID_SESSION_KEY = "password_reset_user_id"
RESET_EMAIL_SESSION_KEY = "password_reset_email"
DEV_OTP_SESSION_KEY = "dev_password_reset_otp"
logger = get_logger(__name__)


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not web_identity_service.is_authenticated():
            flash("Please log in to continue.", "error")
            return redirect(url_for("auth.login"))
        return view_func(*args, **kwargs)

    return wrapper


def _is_admin_role(user: dict | None) -> bool:
    if not user:
        return False
    return str(user.get("user_role") or "") in {"admin", "super_admin"} or bool(user.get("is_admin"))


def _clear_password_reset_session() -> None:
    session.pop(RESET_REQUEST_ID_SESSION_KEY, None)
    session.pop(RESET_TOKEN_SESSION_KEY, None)
    session.pop(RESET_USER_ID_SESSION_KEY, None)
    session.pop(RESET_EMAIL_SESSION_KEY, None)
    session.pop(DEV_OTP_SESSION_KEY, None)


@auth_blueprint.route("/register", methods=["GET", "POST"])
def register():
    existing_user = web_identity_service.get_authenticated_user()
    if existing_user:
        if _is_admin_role(existing_user):
            return redirect(url_for("admin.admin_dashboard"))
        return redirect(url_for("auth.dashboard"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "")
        email = request.form.get("email", "")
        phone_number = request.form.get("phone_number", "")
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not full_name.strip():
            flash("Please enter your name.", "error")
            return redirect(url_for("auth.register"))
        if "@" not in email:
            flash("Please enter a valid email address.", "error")
            return redirect(url_for("auth.register"))
        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "error")
            return redirect(url_for("auth.register"))
        if password != confirm_password:
            flash("Password and confirm password do not match.", "error")
            return redirect(url_for("auth.register"))

        user, error = web_identity_service.register(full_name, email, phone_number, password)
        if error:
            flash(error, "error")
            return redirect(url_for("auth.register"))

        web_identity_service.set_authenticated_user(user)
        flash("Registration successful.", "success")
        return redirect(url_for("auth.dashboard"))

    return render_template(
        "register.html",
        page_title="Register",
        support_telegram=SUPPORT_TELEGRAM,
        super_admin_email=SUPER_ADMIN_EMAIL,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@auth_blueprint.route("/login", methods=["GET", "POST"])
def login():
    try:
        existing_user = web_identity_service.get_authenticated_user()
        if existing_user:
            if _is_admin_role(existing_user):
                return redirect(url_for("admin.admin_dashboard"))
            return redirect(url_for("auth.dashboard"))

        if request.method == "POST":
            login_identifier = request.form.get("login_identifier", "")
            password = request.form.get("password", "")
            user = web_identity_service.authenticate(login_identifier, password)
            if not user:
                flash("Invalid login credentials.", "error")
                return redirect(url_for("auth.login"))

            web_identity_service.set_authenticated_user(user)
            _clear_password_reset_session()
            flash("Login successful.", "success")
            if _is_admin_role(user):
                return redirect(url_for("admin.admin_dashboard"))
            return redirect(url_for("auth.dashboard"))

        return render_template(
            "login.html",
            page_title="Login",
            support_telegram=SUPPORT_TELEGRAM,
            super_admin_email=SUPER_ADMIN_EMAIL,
            admin_authenticated=web_identity_service.is_admin_authenticated(),
        )
    except Exception:
        logger.exception(
            "Website login route failed | method=%s login_identifier=%s remote_addr=%s",
            request.method,
            (request.form.get("login_identifier", "") if request.method == "POST" else ""),
            request.headers.get("X-Forwarded-For", request.remote_addr),
        )
        flash("Login is temporarily unavailable. Please try again shortly.", "error")
        return redirect(url_for("auth.login"))


@auth_blueprint.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        try:
            result = web_password_reset_service.request_reset(
                request.form.get("email", ""),
                requested_ip=request.headers.get("X-Forwarded-For", request.remote_addr),
            )
        except Exception:
            logger.exception(
                "Forgot password route failed | email=%s remote_addr=%s",
                request.form.get("email", ""),
                request.headers.get("X-Forwarded-For", request.remote_addr),
            )
            flash("We could not start password reset right now. Please try again in a few minutes.", "error")
            return redirect(url_for("auth.forgot_password"))

        if not result.get("ok"):
            flash(result.get("error") or "Unable to start password reset.", "error")
            return redirect(url_for("auth.forgot_password"))

        session[RESET_EMAIL_SESSION_KEY] = result.get("email") or ""
        if result.get("dev_otp") and web_password_reset_service.is_local_dev_mode():
            session[DEV_OTP_SESSION_KEY] = result["dev_otp"]
        flash(result.get("message") or "If the email can receive messages, a password reset OTP has been sent.", "success")
        return redirect(url_for("auth.verify_reset_otp", email=result.get("email") or ""))

    dev_otp = None
    if web_password_reset_service.is_local_dev_mode():
        dev_otp = session.get(DEV_OTP_SESSION_KEY)
    else:
        session.pop(DEV_OTP_SESSION_KEY, None)

    return render_template(
        "forgot_password.html",
        page_title="Forgot Password",
        support_telegram=SUPPORT_TELEGRAM,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
        prefilled_email=session.get(RESET_EMAIL_SESSION_KEY, ""),
        dev_otp=dev_otp,
    )


@auth_blueprint.route("/verify-reset-otp", methods=["GET", "POST"])
def verify_reset_otp():
    if request.method == "POST":
        result = web_password_reset_service.verify_otp(
            request.form.get("email", ""),
            request.form.get("otp", ""),
        )
        if not result.get("ok"):
            flash(result.get("error") or "OTP verification failed.", "error")
            return redirect(url_for("auth.verify_reset_otp", email=request.form.get("email", "")))

        session[RESET_REQUEST_ID_SESSION_KEY] = result["reset_id"]
        session[RESET_TOKEN_SESSION_KEY] = result["reset_token"]
        session[RESET_USER_ID_SESSION_KEY] = int(result["user_id"])
        session[RESET_EMAIL_SESSION_KEY] = result["email"]
        session.pop(DEV_OTP_SESSION_KEY, None)
        flash("OTP verified. You can set a new password now.", "success")
        return redirect(url_for("auth.reset_password"))

    dev_otp = None
    if web_password_reset_service.is_local_dev_mode():
        dev_otp = session.get(DEV_OTP_SESSION_KEY)

    return render_template(
        "verify_reset_otp.html",
        page_title="Verify OTP",
        support_telegram=SUPPORT_TELEGRAM,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
        prefilled_email=request.args.get("email") or session.get(RESET_EMAIL_SESSION_KEY, ""),
        dev_otp=dev_otp,
    )


@auth_blueprint.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    reset_request_id = session.get(RESET_REQUEST_ID_SESSION_KEY)
    reset_token = session.get(RESET_TOKEN_SESSION_KEY)
    reset_user_id = session.get(RESET_USER_ID_SESSION_KEY)
    reset_email = session.get(RESET_EMAIL_SESSION_KEY, "")
    if not reset_request_id or not reset_token or not reset_user_id:
        flash("Verify OTP first before setting a new password.", "error")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "error")
            return redirect(url_for("auth.reset_password"))
        if password != confirm_password:
            flash("Password and confirm password do not match.", "error")
            return redirect(url_for("auth.reset_password"))

        result = web_password_reset_service.reset_password(
            reset_id=str(reset_request_id),
            reset_token=str(reset_token),
            user_id=int(reset_user_id),
            new_password=password,
        )
        if not result.get("ok"):
            flash(result.get("error") or "Password reset failed.", "error")
            return redirect(url_for("auth.reset_password"))

        _clear_password_reset_session()
        flash("Password reset successful. Please log in with your new password.", "success")
        return redirect(url_for("auth.login"))

    return render_template(
        "reset_password.html",
        page_title="Reset Password",
        support_telegram=SUPPORT_TELEGRAM,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
        reset_email=reset_email,
    )


@auth_blueprint.route("/logout", methods=["GET", "POST"])
def logout():
    web_identity_service.logout_user()
    _clear_password_reset_session()
    flash("You have been logged out.", "success")
    return redirect(url_for("pages.home"))


@auth_blueprint.route("/dashboard")
@login_required
def dashboard():
    user = web_identity_service.get_authenticated_user()
    if _is_admin_role(user):
        return redirect(url_for("admin.admin_dashboard"))

    performance = web_quiz_service.user_performance_snapshot(user["user_id"])
    return render_template(
        "dashboard.html",
        page_title="Dashboard",
        user=user,
        performance=performance,
        support_telegram=SUPPORT_TELEGRAM,
        admin_authenticated=False,
    )
