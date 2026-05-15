import hashlib
import hmac
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from werkzeug.security import generate_password_hash

from config import (
    APP_ENV,
    PASSWORD_RESET_LOCAL_DEV_OTP,
    PASSWORD_RESET_OTP_TTL_MINUTES,
    SECRET_KEY,
    SITE_NAME,
    SMTP_FROM_EMAIL,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USERNAME,
    SMTP_USE_TLS,
)
from db.database import database
from services.user_service_db import now_iso, parse_utc_datetime, user_service
from utils.logging_utils import get_logger


logger = get_logger(__name__)


class WebPasswordResetService:
    def request_reset(self, email: str, *, requested_ip: str | None = None) -> dict:
        normalized_email = self._normalize_email(email)
        generic_message = "If the email can receive messages, a password reset OTP has been sent."
        if not normalized_email:
            return {"ok": False, "error": "Please enter a valid email address."}

        user = user_service.find_by_email(normalized_email)
        if not user or not user.get("password_hash"):
            logger.info("Password reset requested for missing or unsupported account | email=%s", normalized_email)
            return {
                "ok": True,
                "message": generic_message,
                "email": normalized_email,
                "dev_otp": None,
            }

        reset_id = secrets.token_urlsafe(18)
        otp = f"{secrets.randbelow(1000000):06d}"
        created_at = self._now()
        expires_at = created_at + timedelta(minutes=PASSWORD_RESET_OTP_TTL_MINUTES)
        logger.info(
            "Password reset OTP generated | user_id=%s email=%s reset_id=%s expires_at=%s",
            user.get("user_id"),
            normalized_email,
            reset_id,
            expires_at.replace(microsecond=0).isoformat(),
        )

        if self.is_local_dev_mode():
            logger.info("Local password reset OTP | email=%s otp=%s expires_at=%s", normalized_email, otp, expires_at.isoformat())
            print(f"[DEV OTP] Password reset OTP for {normalized_email}: {otp}")
        else:
            try:
                self._send_reset_email(normalized_email, otp, expires_at)
            except Exception:
                logger.exception(
                    "Password reset email send failed | user_id=%s email=%s reset_id=%s",
                    user.get("user_id"),
                    normalized_email,
                    reset_id,
                )
                return {
                    "ok": False,
                    "error": "We could not send the reset email right now. Please try again in a few minutes.",
                }

        otp_hash = self._hash_secret(reset_id, otp)
        with database.connection() as conn:
            conn.execute(
                """
                UPDATE password_reset_requests
                SET used_at = COALESCE(used_at, ?)
                WHERE user_id = ? AND password_reset_at IS NULL
                """,
                (now_iso(), user["user_id"]),
            )
            conn.execute(
                """
                INSERT INTO password_reset_requests (
                    reset_id, user_id, email, otp_hash, expires_at, created_at, requested_ip
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reset_id,
                    user["user_id"],
                    normalized_email,
                    otp_hash,
                    expires_at.replace(microsecond=0).isoformat(),
                    created_at.replace(microsecond=0).isoformat(),
                    requested_ip,
                ),
            )

        logger.info(
            "Password reset OTP stored | user_id=%s email=%s reset_id=%s expires_at=%s",
            user.get("user_id"),
            normalized_email,
            reset_id,
            expires_at.replace(microsecond=0).isoformat(),
        )
        return {
            "ok": True,
            "message": generic_message,
            "email": normalized_email,
            "dev_otp": otp if self.is_local_dev_mode() else None,
        }

    def verify_otp(self, email: str, otp: str) -> dict:
        normalized_email = self._normalize_email(email)
        normalized_otp = "".join(character for character in (otp or "") if character.isdigit())
        if not normalized_email:
            return {"ok": False, "error": "Please enter a valid email address."}
        if len(normalized_otp) != 6:
            return {"ok": False, "error": "Please enter the 6 digit OTP."}

        with database.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM password_reset_requests
                WHERE email = ? AND password_reset_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (normalized_email,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "Invalid or expired OTP."}

            request_row = dict(row)
            if request_row.get("used_at") or request_row.get("otp_verified_at"):
                return {"ok": False, "error": "This OTP has already been used. Request a new one."}

            expires_at = parse_utc_datetime(request_row.get("expires_at"))
            if not expires_at or expires_at <= self._now():
                conn.execute(
                    "UPDATE password_reset_requests SET used_at = COALESCE(used_at, ?) WHERE reset_id = ?",
                    (now_iso(), request_row["reset_id"]),
                )
                return {"ok": False, "error": "OTP expired. Please request a new one."}

            if not hmac.compare_digest(str(request_row.get("otp_hash") or ""), self._hash_secret(request_row["reset_id"], normalized_otp)):
                return {"ok": False, "error": "Invalid or expired OTP."}

            reset_token = secrets.token_urlsafe(32)
            reset_expires_at = self._now() + timedelta(minutes=PASSWORD_RESET_OTP_TTL_MINUTES)
            conn.execute(
                """
                UPDATE password_reset_requests
                SET otp_verified_at = ?,
                    used_at = ?,
                    reset_token_hash = ?,
                    reset_expires_at = ?
                WHERE reset_id = ?
                """,
                (
                    now_iso(),
                    now_iso(),
                    self._hash_secret(request_row["reset_id"], reset_token),
                    reset_expires_at.replace(microsecond=0).isoformat(),
                    request_row["reset_id"],
                ),
            )

        return {
            "ok": True,
            "reset_id": request_row["reset_id"],
            "reset_token": reset_token,
            "user_id": request_row["user_id"],
            "email": normalized_email,
        }

    def reset_password(self, *, reset_id: str, reset_token: str, user_id: int, new_password: str) -> dict:
        cleaned_password = (new_password or "").strip()
        if len(cleaned_password) < 6:
            return {"ok": False, "error": "Password must be at least 6 characters long."}

        with database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM password_reset_requests WHERE reset_id = ? LIMIT 1",
                (reset_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "Password reset session is invalid. Please restart the flow."}

            request_row = dict(row)
            if int(request_row.get("user_id") or 0) != int(user_id):
                return {"ok": False, "error": "Password reset session is invalid. Please restart the flow."}
            if request_row.get("password_reset_at"):
                return {"ok": False, "error": "This password reset session has already been used."}
            if not request_row.get("reset_token_hash"):
                return {"ok": False, "error": "Password reset session is invalid. Please restart the flow."}

            reset_expires_at = parse_utc_datetime(request_row.get("reset_expires_at"))
            if not reset_expires_at or reset_expires_at <= self._now():
                return {"ok": False, "error": "Password reset session expired. Please request a new OTP."}

            if not hmac.compare_digest(str(request_row.get("reset_token_hash") or ""), self._hash_secret(reset_id, reset_token)):
                return {"ok": False, "error": "Password reset session is invalid. Please restart the flow."}

            user = user_service.get_user(int(user_id))
            if not user or self._normalize_email(user.get("email")) != self._normalize_email(request_row.get("email")):
                return {"ok": False, "error": "The linked account could not be verified."}

            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE user_id = ?",
                (generate_password_hash(cleaned_password), now_iso(), int(user_id)),
            )
            conn.execute(
                """
                UPDATE password_reset_requests
                SET password_reset_at = ?, reset_token_hash = NULL
                WHERE reset_id = ?
                """,
                (now_iso(), reset_id),
            )

        logger.info("Password reset completed | user_id=%s reset_id=%s", user_id, reset_id)
        return {"ok": True}

    def is_local_dev_mode(self) -> bool:
        return PASSWORD_RESET_LOCAL_DEV_OTP or APP_ENV != "production"

    def _send_reset_email(self, recipient_email: str, otp: str, expires_at: datetime) -> None:
        if not SMTP_HOST or not SMTP_PORT or not SMTP_FROM_EMAIL:
            raise ValueError("SMTP is not configured for password reset emails.")
        if SMTP_USERNAME and not SMTP_PASSWORD:
            raise ValueError("SMTP username is configured but SMTP password is missing.")

        logger.info(
            "Password reset email send attempted | email=%s smtp_host=%s smtp_port=%s tls=%s smtp_user_configured=%s from_email=%s",
            recipient_email,
            SMTP_HOST,
            SMTP_PORT,
            SMTP_USE_TLS,
            bool(SMTP_USERNAME),
            SMTP_FROM_EMAIL,
        )

        message = EmailMessage()
        message["Subject"] = f"{SITE_NAME} password reset OTP"
        message["From"] = SMTP_FROM_EMAIL
        message["To"] = recipient_email
        message.set_content(
            (
                f"Your {SITE_NAME} password reset OTP is {otp}.\n\n"
                f"It expires at {expires_at.replace(microsecond=0).isoformat()} UTC and can be used only once.\n"
                "If you did not request this, you can ignore this email."
            )
        )

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            if SMTP_USE_TLS:
                smtp.starttls()
                smtp.ehlo()
            if SMTP_USERNAME:
                smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)

        logger.info("Password reset email send succeeded | email=%s", recipient_email)

    def _hash_secret(self, reset_id: str, value: str) -> str:
        return hmac.new(
            SECRET_KEY.encode("utf-8"),
            f"{reset_id}:{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _normalize_email(self, email: str | None) -> str:
        normalized = (email or "").strip().lower()
        return normalized if normalized and "@" in normalized else ""

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)


web_password_reset_service = WebPasswordResetService()
