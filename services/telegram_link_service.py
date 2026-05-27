import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from config import BOT_URL, BOT_USERNAME, TELEGRAM_LINK_TOKEN_TTL_MINUTES
from db.database import database
from services.user_service_db import now_iso, parse_utc_datetime, user_service
from utils.logging_utils import get_logger


logger = get_logger(__name__)


class TelegramLinkService:
    _USER_ID_TABLES = (
        "payment_orders",
        "payments",
        "quiz_attempts",
        "question_reports",
        "support_messages",
        "password_reset_requests",
    )

    def create_link_request(self, website_user_id: int, *, allow_relink: bool = False) -> dict:
        website_user = user_service.get_user(website_user_id)
        if not website_user:
            return {"ok": False, "error": "Website account not found."}

        token = secrets.token_urlsafe(24)
        token_id = uuid.uuid4().hex
        token_hash = self._hash_token(token)
        created_at = datetime.now(timezone.utc).replace(microsecond=0)
        expires_at = created_at + timedelta(minutes=TELEGRAM_LINK_TOKEN_TTL_MINUTES)

        with database.connection() as conn:
            conn.execute(
                """
                INSERT INTO telegram_link_tokens (
                    token_id,
                    website_user_id,
                    token_hash,
                    expires_at,
                    allow_relink,
                    used_at,
                    consumed_by_telegram_user_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    token_id,
                    int(website_user_id),
                    token_hash,
                    expires_at.isoformat(),
                    1 if allow_relink else 0,
                    created_at.isoformat(),
                ),
            )

        link = self.get_website_link(website_user_id)
        return {
            "ok": True,
            "token": token,
            "expires_at": expires_at.isoformat(),
            "bot_start_url": self.build_bot_start_url(token),
            "link": link,
            "allow_relink": bool(allow_relink),
        }

    def build_bot_start_url(self, token: str) -> str:
        username = (BOT_USERNAME or "").strip().lstrip("@")
        if username:
            return f"https://t.me/{username}?start={quote(token)}"
        separator = "&" if "?" in BOT_URL else "?"
        return f"{BOT_URL}{separator}start={quote(token)}"

    def get_website_link(self, website_user_id: int) -> dict:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM telegram_account_links WHERE website_user_id = ?",
                (int(website_user_id),),
            ).fetchone()
        return dict(row) if row else {}

    def get_telegram_link(self, telegram_user_id: int) -> dict:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM telegram_account_links WHERE telegram_user_id = ?",
                (int(telegram_user_id),),
            ).fetchone()
        return dict(row) if row else {}

    def resolve_website_user_id(self, telegram_user_id: int) -> int | None:
        link = self.get_telegram_link(telegram_user_id)
        if not link:
            return None
        return int(link["website_user_id"])

    def consume_start_token(self, token: str, tg_user) -> dict:
        raw_token = (token or "").strip()
        if not raw_token:
            return {"ok": False, "status": "missing_token", "message": "Missing link token."}

        token_hash = self._hash_token(raw_token)
        telegram_user_id = int(tg_user.id)
        telegram_username = getattr(tg_user, "username", None)
        telegram_first_name = getattr(tg_user, "first_name", None)
        telegram_full_name = getattr(tg_user, "full_name", None) or getattr(tg_user, "first_name", "") or "Telegram User"

        with database.connection() as conn:
            token_row = conn.execute(
                "SELECT * FROM telegram_link_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not token_row:
                return {
                    "ok": False,
                    "status": "invalid_token",
                    "message": "This Telegram link is invalid. Start again from the website dashboard.",
                }

            token_record = dict(token_row)
            if token_record.get("used_at"):
                return {
                    "ok": False,
                    "status": "used_token",
                    "message": "This Telegram link has already been used. Start again from the website dashboard.",
                }

            expires_at = parse_utc_datetime(token_record.get("expires_at"))
            if not expires_at or expires_at <= datetime.now(timezone.utc):
                return {
                    "ok": False,
                    "status": "expired_token",
                    "message": "This Telegram link expired after 10 minutes. Start again from the website dashboard.",
                }

            website_user_id = int(token_record["website_user_id"])
            website_user = user_service.get_user(website_user_id)
            if not website_user:
                return {
                    "ok": False,
                    "status": "website_user_missing",
                    "message": "The website account for this link could not be found.",
                }

            website_link_row = conn.execute(
                "SELECT * FROM telegram_account_links WHERE website_user_id = ?",
                (website_user_id,),
            ).fetchone()
            telegram_link_row = conn.execute(
                "SELECT * FROM telegram_account_links WHERE telegram_user_id = ?",
                (telegram_user_id,),
            ).fetchone()
            website_link = dict(website_link_row) if website_link_row else {}
            telegram_link = dict(telegram_link_row) if telegram_link_row else {}

            same_pair = (
                website_link
                and telegram_link
                and int(website_link["telegram_user_id"]) == telegram_user_id
                and int(telegram_link["website_user_id"]) == website_user_id
            )
            website_conflict = website_link and int(website_link["telegram_user_id"]) != telegram_user_id
            telegram_conflict = telegram_link and int(telegram_link["website_user_id"]) != website_user_id
            allow_relink = bool(token_record.get("allow_relink"))

            if (website_conflict or telegram_conflict) and not allow_relink:
                return {
                    "ok": False,
                    "status": "confirmation_required",
                    "message": (
                        "This link would replace an existing Telegram connection. "
                        "Return to the website and use Confirm relink to continue."
                    ),
                    "website_conflict": bool(website_conflict),
                    "telegram_conflict": bool(telegram_conflict),
                }

            if website_conflict:
                conn.execute(
                    "DELETE FROM telegram_account_links WHERE website_user_id = ?",
                    (website_user_id,),
                )
            if telegram_conflict:
                conn.execute(
                    "DELETE FROM telegram_account_links WHERE telegram_user_id = ?",
                    (telegram_user_id,),
                )

            self._merge_telegram_user_into_website_user(
                conn,
                telegram_user_id,
                website_user_id,
                telegram_username,
                telegram_first_name,
                telegram_full_name,
            )

            timestamp = now_iso()
            if same_pair:
                conn.execute(
                    """
                    UPDATE telegram_account_links
                    SET telegram_username = ?, telegram_first_name = ?, telegram_full_name = ?, updated_at = ?
                    WHERE website_user_id = ? AND telegram_user_id = ?
                    """,
                    (
                        telegram_username,
                        telegram_first_name,
                        telegram_full_name,
                        timestamp,
                        website_user_id,
                        telegram_user_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO telegram_account_links (
                        website_user_id,
                        telegram_user_id,
                        telegram_username,
                        telegram_first_name,
                        telegram_full_name,
                        phone_number,
                        linked_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    ON CONFLICT(website_user_id) DO UPDATE SET
                        telegram_user_id = excluded.telegram_user_id,
                        telegram_username = excluded.telegram_username,
                        telegram_first_name = excluded.telegram_first_name,
                        telegram_full_name = excluded.telegram_full_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        website_user_id,
                        telegram_user_id,
                        telegram_username,
                        telegram_first_name,
                        telegram_full_name,
                        timestamp,
                        timestamp,
                    ),
                )

            conn.execute(
                """
                UPDATE telegram_link_tokens
                SET used_at = ?, consumed_by_telegram_user_id = ?
                WHERE token_id = ?
                """,
                (timestamp, telegram_user_id, token_record["token_id"]),
            )

        logger.info(
            "telegram_link_completed | website_user_id=%s telegram_user_id=%s allow_relink=%s",
            website_user_id,
            telegram_user_id,
            allow_relink,
        )
        return {
            "ok": True,
            "status": "linked",
            "website_user_id": website_user_id,
            "message": "Your Telegram account is now linked to your website account.",
            "link": self.get_website_link(website_user_id),
        }

    def store_optional_phone(self, telegram_user_id: int, phone_number: str, *, contact_user_id: int | None = None) -> dict:
        if contact_user_id is not None and int(contact_user_id) != int(telegram_user_id):
            return {
                "ok": False,
                "status": "contact_mismatch",
                "message": "Please share your own contact if you want to add a phone number.",
            }

        normalized_phone = user_service._normalize_phone(phone_number)
        if not normalized_phone:
            return {
                "ok": False,
                "status": "invalid_phone",
                "message": "That phone number could not be saved.",
            }

        link = self.get_telegram_link(telegram_user_id)
        if not link:
            return {
                "ok": False,
                "status": "not_linked",
                "message": "Link your website account first, then you can optionally share your phone number.",
            }

        website_user_id = int(link["website_user_id"])
        existing_phone_owner = user_service.find_by_phone(normalized_phone)
        if existing_phone_owner and int(existing_phone_owner["user_id"]) != website_user_id:
            return {
                "ok": False,
                "status": "phone_conflict",
                "message": "That phone number is already linked to another account.",
            }

        with database.connection() as conn:
            conn.execute(
                """
                UPDATE telegram_account_links
                SET phone_number = ?, updated_at = ?
                WHERE telegram_user_id = ?
                """,
                (normalized_phone, now_iso(), int(telegram_user_id)),
            )
            conn.execute(
                """
                UPDATE users
                SET phone_number = COALESCE(NULLIF(phone_number, ''), ?),
                    updated_at = ?
                WHERE user_id = ?
                """,
                (normalized_phone, now_iso(), website_user_id),
            )

        return {
            "ok": True,
            "status": "phone_saved",
            "message": "Your phone number was saved as optional secondary verification.",
        }

    def _merge_telegram_user_into_website_user(
        self,
        conn,
        telegram_user_id: int,
        website_user_id: int,
        telegram_username: str | None,
        telegram_first_name: str | None,
        telegram_full_name: str | None,
    ) -> None:
        if int(telegram_user_id) == int(website_user_id):
            conn.execute(
                """
                UPDATE users
                SET telegram_username = COALESCE(NULLIF(?, ''), telegram_username),
                    telegram_first_name = COALESCE(NULLIF(?, ''), telegram_first_name),
                    telegram_full_name = COALESCE(NULLIF(?, ''), telegram_full_name),
                    updated_at = ?
                WHERE user_id = ?
                """,
                (telegram_username, telegram_first_name, telegram_full_name or "Telegram User", now_iso(), website_user_id),
            )
            return

        source = user_service.get_user(telegram_user_id)
        destination = user_service.get_user(website_user_id)
        if not destination:
            return

        if not source:
            conn.execute(
                """
                UPDATE users
                SET telegram_username = COALESCE(NULLIF(?, ''), telegram_username),
                    telegram_first_name = COALESCE(NULLIF(?, ''), telegram_first_name),
                    telegram_full_name = COALESCE(NULLIF(?, ''), telegram_full_name),
                    updated_at = ?
                WHERE user_id = ?
                """,
                (telegram_username, telegram_first_name, telegram_full_name or "Telegram User", now_iso(), website_user_id),
            )
            return

        merged_expiry = self._later_iso(destination.get("premium_expires_at"), source.get("premium_expires_at"))
        merged_daily_date, merged_daily_count = self._merge_daily_question_state(destination, source)

        conn.execute(
            """
            UPDATE users
            SET username = ?,
                website_name = COALESCE(NULLIF(website_name, ''), NULLIF(full_name, ''), ?),
                telegram_username = COALESCE(NULLIF(?, ''), NULLIF(telegram_username, ''), NULLIF(username, '')),
                telegram_first_name = COALESCE(NULLIF(?, ''), NULLIF(telegram_first_name, '')),
                telegram_full_name = COALESCE(NULLIF(?, ''), NULLIF(telegram_full_name, ''), NULLIF(full_name, '')),
                phone_number = COALESCE(NULLIF(phone_number, ''), ?),
                full_name = ?,
                is_admin = ?,
                user_role = ?,
                is_premium = ?,
                premium_expires_at = ?,
                daily_question_date = ?,
                daily_question_count = ?,
                pdf_generation_count = ?,
                quiz_played = ?,
                correct_answers = ?,
                wrong_answers = ?,
                score = ?,
                created_at = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                telegram_username or destination.get("username") or source.get("username"),
                destination.get("website_name") or destination.get("full_name") or source.get("website_name") or source.get("full_name") or "QuizPathshala User",
                telegram_username or source.get("telegram_username") or source.get("username"),
                telegram_first_name or source.get("telegram_first_name"),
                telegram_full_name or source.get("telegram_full_name") or source.get("full_name"),
                source.get("phone_number"),
                destination.get("website_name") or destination.get("full_name") or source.get("website_name") or source.get("full_name") or "QuizPathshala User",
                1 if destination.get("is_admin") else 0,
                str(destination.get("user_role") or "user"),
                1 if destination.get("is_premium") or source.get("is_premium") else 0,
                merged_expiry,
                merged_daily_date,
                merged_daily_count,
                int(destination.get("pdf_generation_count") or 0) + int(source.get("pdf_generation_count") or 0),
                int(destination.get("quiz_played") or 0) + int(source.get("quiz_played") or 0),
                int(destination.get("correct_answers") or 0) + int(source.get("correct_answers") or 0),
                int(destination.get("wrong_answers") or 0) + int(source.get("wrong_answers") or 0),
                float(destination.get("score") or 0) + float(source.get("score") or 0),
                self._earlier_iso(destination.get("created_at"), source.get("created_at")) or now_iso(),
                now_iso(),
                website_user_id,
            ),
        )

        for table_name in self._USER_ID_TABLES:
            conn.execute(
                f"UPDATE {table_name} SET user_id = ? WHERE user_id = ?",
                (website_user_id, telegram_user_id),
            )

        conn.execute("DELETE FROM users WHERE user_id = ?", (telegram_user_id,))

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _later_iso(self, first: str | None, second: str | None) -> str | None:
        first_dt = parse_utc_datetime(first)
        second_dt = parse_utc_datetime(second)
        if first_dt and second_dt:
            return max(first_dt, second_dt).replace(microsecond=0).isoformat()
        return first or second

    def _earlier_iso(self, first: str | None, second: str | None) -> str | None:
        first_dt = parse_utc_datetime(first)
        second_dt = parse_utc_datetime(second)
        if first_dt and second_dt:
            return min(first_dt, second_dt).replace(microsecond=0).isoformat()
        return first or second

    def _merge_daily_question_state(self, destination: dict, source: dict) -> tuple[str | None, int]:
        destination_date = destination.get("daily_question_date")
        source_date = source.get("daily_question_date")
        destination_count = int(destination.get("daily_question_count") or 0)
        source_count = int(source.get("daily_question_count") or 0)

        if destination_date and source_date:
            if destination_date == source_date:
                return destination_date, destination_count + source_count
            if destination_date > source_date:
                return destination_date, destination_count
            return source_date, source_count
        if destination_date:
            return destination_date, destination_count
        if source_date:
            return source_date, source_count
        return None, destination_count + source_count

    def _preferred_role(self, destination_role: str | None, source_role: str | None) -> str:
        priority = {"user": 0, "admin": 1, "super_admin": 2}
        destination_value = priority.get(str(destination_role or "user"), 0)
        source_value = priority.get(str(source_role or "user"), 0)
        return str(destination_role or source_role or "user") if destination_value >= source_value else str(source_role)


telegram_link_service = TelegramLinkService()
