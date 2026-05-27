from datetime import date, datetime, timezone
import uuid

from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    ADMINS,
    ADMIN_LOGIN_IDENTIFIER,
    DEMO_USER_LOGIN,
    DEMO_USER_NAME,
    DEMO_USER_PASSWORD,
    SUPER_ADMIN_EMAIL,
    SUPER_ADMIN_NAME,
    SUPER_ADMIN_PASSWORD,
    SUPREME_ADMIN_ID,
)
from db.database import database
from utils.logging_utils import get_logger


logger = get_logger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_utc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


class UserService:
    def ensure_profile(
        self,
        *,
        user_id: int,
        full_name: str,
        username: str | None = None,
        website_name: str | None = None,
        telegram_first_name: str | None = None,
        telegram_username: str | None = None,
        telegram_full_name: str | None = None,
        is_admin: bool = False,
    ) -> dict:
        timestamp = now_iso()
        computed_is_admin = 1 if (is_admin or self.is_admin(user_id)) else 0
        computed_role = "admin" if computed_is_admin else "user"
        cleaned_full_name = self._clean_full_name(full_name)
        cleaned_website_name = None if website_name is None else (self._clean_full_name(website_name) or cleaned_full_name)
        cleaned_telegram_first_name = self._clean_full_name(telegram_first_name)
        cleaned_telegram_full_name = self._clean_full_name(telegram_full_name)

        with database.connection() as conn:
            row = conn.execute(
                "SELECT user_id, user_role FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            preserved_role = row["user_role"] if row and row["user_role"] else computed_role
            if row:
                conn.execute(
                    """
                    UPDATE users
                    SET username = ?,
                        full_name = ?,
                        website_name = COALESCE(NULLIF(?, ''), website_name, full_name),
                        telegram_first_name = COALESCE(NULLIF(?, ''), telegram_first_name),
                        telegram_username = COALESCE(NULLIF(?, ''), telegram_username),
                        telegram_full_name = COALESCE(NULLIF(?, ''), telegram_full_name),
                        is_admin = ?,
                        user_role = ?,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        username,
                        cleaned_full_name,
                        cleaned_website_name,
                        cleaned_telegram_first_name,
                        telegram_username,
                        cleaned_telegram_full_name,
                        computed_is_admin,
                        preserved_role,
                        timestamp,
                        user_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, username, full_name, website_name, telegram_first_name,
                        telegram_username, telegram_full_name, is_admin, user_role, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        cleaned_full_name,
                        cleaned_website_name or cleaned_full_name,
                        cleaned_telegram_first_name,
                        telegram_username,
                        cleaned_telegram_full_name,
                        computed_is_admin,
                        preserved_role,
                        timestamp,
                        timestamp,
                    ),
                )

        return self.get_user(user_id)

    def find_by_login_identifier(self, login_identifier: str) -> dict:
        normalized_identifier = self._normalize_login_identifier(login_identifier)
        if not normalized_identifier:
            return {}

        with database.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM users
                WHERE login_identifier = ? OR email = ? OR phone_number = ?
                ORDER BY CASE WHEN login_identifier = ? THEN 0 WHEN email = ? THEN 1 ELSE 2 END
                LIMIT 1
                """,
                (
                    normalized_identifier,
                    normalized_identifier,
                    normalized_identifier,
                    normalized_identifier,
                    normalized_identifier,
                ),
            ).fetchone()
        return self._normalize_user_dict(dict(row)) if row else {}

    def find_by_email(self, email: str) -> dict:
        normalized_email = self._normalize_email(email)
        if not normalized_email:
            return {}

        with database.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (normalized_email,)).fetchone()
        return self._normalize_user_dict(dict(row)) if row else {}

    def find_by_phone(self, phone_number: str) -> dict:
        normalized_phone = self._normalize_phone(phone_number)
        if not normalized_phone:
            return {}

        with database.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE phone_number = ?", (normalized_phone,)).fetchone()
        return self._normalize_user_dict(dict(row)) if row else {}

    def count_registered_accounts(self) -> int:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM users WHERE password_hash IS NOT NULL AND email IS NOT NULL"
            ).fetchone()
        return int(row["total"] or 0)

    def authenticate_web_user(self, login_identifier: str, password: str) -> dict:
        user = self.find_by_login_identifier(login_identifier)
        if not user:
            return {}

        password_hash = user.get("password_hash")
        if not password_hash or not password:
            return {}

        try:
            if not check_password_hash(password_hash, password):
                return {}
        except ValueError:
            return {}

        return user

    def register_web_user(
        self,
        *,
        full_name: str,
        email: str,
        phone_number: str | None,
        password: str,
    ) -> tuple[dict, str | None]:
        cleaned_name = self._clean_full_name(full_name)
        normalized_email = self._normalize_email(email)
        normalized_phone = self._normalize_phone(phone_number)

        if not cleaned_name:
            return {}, "Please enter your full name."
        if not normalized_email:
            return {}, "Please enter a valid email address."
        if not password:
            return {}, "Please enter a password."
        if self.find_by_email(normalized_email):
            return {}, "An account with this email already exists."
        if normalized_phone and self.find_by_phone(normalized_phone):
            return {}, "This mobile number is already linked to another account."

        registered_accounts = self.count_registered_accounts()
        if registered_accounts == 0 and normalized_email != SUPER_ADMIN_EMAIL:
            return {}, f"The first website account must be registered with {SUPER_ADMIN_EMAIL}."

        role = "super_admin" if registered_accounts == 0 else "user"
        timestamp = now_iso()
        password_hash = generate_password_hash(password)

        with database.connection() as conn:
            if self._users_table_has_integer_primary_key(conn):
                cursor = conn.execute(
                    """
                    INSERT INTO users (
                        username,
                        login_identifier,
                        email,
                        phone_number,
                        password_hash,
                        full_name,
                        website_name,
                        is_admin,
                        user_role,
                        created_at,
                        updated_at
                    ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_email,
                        normalized_email,
                        normalized_phone,
                        password_hash,
                        cleaned_name,
                        cleaned_name,
                        1 if role == "super_admin" else 0,
                        role,
                        timestamp,
                        timestamp,
                    ),
                )
                user_id = int(cursor.lastrowid)
            else:
                generated_user_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id,
                        username,
                        login_identifier,
                        email,
                        phone_number,
                        password_hash,
                        full_name,
                        website_name,
                        is_admin,
                        user_role,
                        created_at,
                        updated_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generated_user_id,
                        normalized_email,
                        normalized_email,
                        normalized_phone,
                        password_hash,
                        cleaned_name,
                        cleaned_name,
                        1 if role == "super_admin" else 0,
                        role,
                        timestamp,
                        timestamp,
                    ),
                )
                user_id = generated_user_id

        return self.get_user(user_id), None

    def upsert_login_account(
        self,
        *,
        login_identifier: str,
        password: str,
        full_name: str,
        role: str = "user",
        user_id: int | None = None,
        phone_number: str | None = None,
    ) -> dict:
        normalized_identifier = self._normalize_login_identifier(login_identifier)
        normalized_email = self._normalize_email(login_identifier)
        normalized_phone = self._normalize_phone(phone_number)
        normalized_role = role if role in {"admin", "super_admin"} else "user"
        if not normalized_identifier or not password:
            return {}

        existing = self.find_by_login_identifier(normalized_identifier)
        timestamp = now_iso()
        resolved_user_id = existing.get("user_id") if existing else (user_id or self._generate_persistent_user_id())
        password_hash = generate_password_hash(password)

        with database.connection() as conn:
            row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (resolved_user_id,)).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE users
                    SET login_identifier = ?, email = ?, phone_number = ?, password_hash = ?, full_name = ?, website_name = ?, is_admin = ?, user_role = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        normalized_identifier,
                        normalized_email,
                        normalized_phone,
                        password_hash,
                        self._clean_full_name(full_name),
                        self._clean_full_name(full_name),
                        1 if normalized_role in {"admin", "super_admin"} else 0,
                        normalized_role,
                        timestamp,
                        resolved_user_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, username, login_identifier, email, phone_number, password_hash, full_name, website_name, is_admin, user_role, created_at, updated_at
                    ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_user_id,
                        normalized_identifier,
                        normalized_email,
                        normalized_phone,
                        password_hash,
                        self._clean_full_name(full_name),
                        self._clean_full_name(full_name),
                        1 if normalized_role in {"admin", "super_admin"} else 0,
                        normalized_role,
                        timestamp,
                        timestamp,
                    ),
                )

        return self.get_user(int(resolved_user_id))

    def ensure_default_web_accounts(self) -> None:
        if SUPER_ADMIN_PASSWORD:
            self.upsert_login_account(
                login_identifier=SUPER_ADMIN_EMAIL,
                password=SUPER_ADMIN_PASSWORD,
                full_name=SUPER_ADMIN_NAME,
                role="super_admin",
                user_id=SUPREME_ADMIN_ID,
            )

        if DEMO_USER_LOGIN and DEMO_USER_PASSWORD:
            self.upsert_login_account(
                login_identifier=DEMO_USER_LOGIN,
                password=DEMO_USER_PASSWORD,
                full_name=DEMO_USER_NAME,
                role="user",
            )

    def admin_storage_ready(self) -> bool:
        return database.table_exists("users")

    def initialize_admin_storage(self):
        logger.info(
            "Admin access configured from config only | supreme_admin_id=%s admins=%s",
            SUPREME_ADMIN_ID,
            sorted(ADMINS),
        )

    def is_supreme_admin(self, user_id: int) -> bool:
        resolved_user_id = self._coerce_user_id(user_id)
        if resolved_user_id is None:
            return False

        if resolved_user_id == int(SUPREME_ADMIN_ID):
            return True

        internal_user_id = self._resolve_linked_website_user_id(resolved_user_id)
        with database.connection() as conn:
            row = conn.execute(
                "SELECT user_role FROM users WHERE user_id = ?",
                (internal_user_id,),
            ).fetchone()
        return bool(row and str(row["user_role"] or "") == "super_admin")

    def is_admin(self, user_id: int) -> bool:
        resolved_user_id = self._coerce_user_id(user_id)
        if resolved_user_id is None:
            return False

        if resolved_user_id == int(SUPREME_ADMIN_ID) or resolved_user_id in ADMINS:
            return True

        internal_user_id = self._resolve_linked_website_user_id(resolved_user_id)
        with database.connection() as conn:
            row = conn.execute(
                "SELECT is_admin, user_role FROM users WHERE user_id = ?",
                (internal_user_id,),
            ).fetchone()
        if not row:
            return False

        return bool(int(row["is_admin"] or 0)) or str(row["user_role"] or "") in {"admin", "super_admin"}

    def ensure_user(self, tg_user) -> dict:
        from services.telegram_link_service import telegram_link_service

        linked_user_id = telegram_link_service.resolve_website_user_id(tg_user.id)
        resolved_user_id = int(linked_user_id) if linked_user_id is not None else int(tg_user.id)
        return self.ensure_profile(
            user_id=resolved_user_id,
            username=tg_user.username,
            full_name=tg_user.full_name,
            website_name=(tg_user.full_name if linked_user_id is None else None),
            telegram_first_name=getattr(tg_user, "first_name", None),
            telegram_username=tg_user.username,
            telegram_full_name=tg_user.full_name,
        )

    def get_user(self, user_id: int) -> dict:
        with database.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

        if row:
            user = self._normalize_user_dict(dict(row))
        elif int(user_id) == int(SUPREME_ADMIN_ID):
            user = {
                "user_id": user_id,
                "username": None,
                "login_identifier": SUPER_ADMIN_EMAIL,
                "email": SUPER_ADMIN_EMAIL,
                "phone_number": None,
                "password_hash": None,
                "user_role": "super_admin",
                "full_name": "Supreme Admin",
                "is_admin": 1,
                "is_premium": 0,
                "premium_expires_at": None,
                "daily_question_date": None,
                "daily_question_count": 0,
                "pdf_generation_count": 0,
                "quiz_played": 0,
                "correct_answers": 0,
                "wrong_answers": 0,
                "score": 0,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        else:
            user = {}

        if user and self.is_admin(user_id) and user.get("user_role") != "super_admin":
            user["is_admin"] = 1
            user["user_role"] = "admin"
        return self._normalize_premium_status(user) if user else {}

    def list_users(self) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [self._normalize_premium_status(self._normalize_user_dict(dict(row))) for row in rows]

    def get_leaderboard(self, limit: int = 10) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM users
                ORDER BY score DESC, correct_answers DESC, full_name ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._normalize_premium_status(self._normalize_user_dict(dict(row))) for row in rows]

    def list_admins(self) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute("SELECT * FROM users WHERE is_admin = 1 ORDER BY full_name ASC").fetchall()
        admins = [self._normalize_premium_status(self._normalize_user_dict(dict(row))) for row in rows]
        if not any(item["user_id"] == SUPREME_ADMIN_ID for item in admins):
            admins.insert(0, self.get_user(SUPREME_ADMIN_ID))
        return admins

    def list_non_admins(self) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute("SELECT * FROM users WHERE is_admin = 0 ORDER BY full_name ASC").fetchall()
        return [self._normalize_premium_status(self._normalize_user_dict(dict(row))) for row in rows]

    def get_admin_debug_info(self, user_id: int) -> dict:
        with database.connection() as conn:
            row = conn.execute("SELECT user_id, is_admin FROM users WHERE user_id = ?", (user_id,)).fetchone()

        exists = bool(row)
        is_admin_column = int(row["is_admin"]) if row and row["is_admin"] is not None else None
        is_supreme = self.is_supreme_admin(user_id)
        final_access_allowed = is_supreme or is_admin_column == 1

        return {
            "user_id": user_id,
            "exists": exists,
            "is_admin_column": is_admin_column,
            "is_supreme_admin": is_supreme,
            "final_access_allowed": final_access_allowed,
        }

    def set_admin_status(self, user_id: int, is_admin: bool):
        if self.is_supreme_admin(user_id):
            return self.get_user(user_id)

        with database.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET is_admin = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (1 if is_admin else 0, now_iso(), user_id),
            )
        return self.get_user(user_id)

    def promote_to_admin(self, user_id: int) -> tuple[dict | None, str]:
        if not self.admin_storage_ready():
            raise RuntimeError("Admins table was not found.")

        if self.is_supreme_admin(user_id):
            return self.get_user(user_id), "supreme_admin"

        existing = self.get_user(user_id)
        if existing and existing.get("is_admin"):
            logger.info("Promote admin skipped | target_user_id=%s reason=already_admin", user_id)
            return existing, "already_admin"

        action = "updated" if existing else "inserted"
        with database.connection() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    user_id,
                    username,
                    full_name,
                    is_admin,
                    created_at,
                    updated_at
                )
                VALUES (?, NULL, ?, 1, datetime('now'), datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    is_admin = 1,
                    user_role = 'admin',
                    updated_at = datetime('now')
                """,
                (user_id, f"Admin {user_id}"),
            )

        if action == "inserted":
            logger.info("Promote admin success | target_user_id=%s row_inserted=1 placeholder_created=1", user_id)
        else:
            logger.info("Promote admin success | target_user_id=%s row_updated=1", user_id)

        return self.get_user(user_id), action

    def demote_admin(self, user_id: int) -> dict | None:
        if self.is_supreme_admin(user_id):
            return None

        user = self.get_user(user_id)
        if not user or not user.get("is_admin"):
            return None

        with database.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET is_admin = 0, user_role = 'user', updated_at = ?
                WHERE user_id = ?
                """,
                (now_iso(), user_id),
            )
        return self.get_user(user_id)

    def record_quiz_start(self, user_id: int):
        with database.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET quiz_played = quiz_played + 1,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (now_iso(), user_id),
            )

    def record_answer(self, user_id: int, correct: bool):
        with database.connection() as conn:
            if correct:
                conn.execute(
                    """
                    UPDATE users
                    SET correct_answers = correct_answers + 1,
                        score = score + 1,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now_iso(), user_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET wrong_answers = wrong_answers + 1,
                        score = score - 0.25,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now_iso(), user_id),
                )

    def record_quiz_attempt(
        self,
        user_id: int,
        set_id: int,
        requested_count: int,
        correct_count: int,
        wrong_count: int,
        skipped_count: int,
        ended_reason: str,
    ):
        with database.connection() as conn:
            conn.execute(
                """
                INSERT INTO quiz_attempts (
                    user_id, set_id, requested_count, correct_count, wrong_count,
                    skipped_count, ended_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    set_id,
                    requested_count,
                    correct_count,
                    wrong_count,
                    skipped_count,
                    ended_reason,
                    now_iso(),
                ),
            )

    def sync_json_user_stats(self, users: list[dict]):
        timestamp = now_iso()

        with database.connection() as conn:
            for user in users:
                if not isinstance(user, dict) or "id" not in user:
                    continue

                conn.execute(
                    """
                    INSERT INTO users (
                        user_id, username, full_name, is_admin, is_premium,
                        premium_expires_at, daily_question_date, daily_question_count, pdf_generation_count,
                        quiz_played, correct_answers, wrong_answers, score, created_at, updated_at
                    ) VALUES (?, NULL, ?, ?, 0, NULL, NULL, 0, 0, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        full_name = excluded.full_name,
                        quiz_played = excluded.quiz_played,
                        correct_answers = excluded.correct_answers,
                        wrong_answers = excluded.wrong_answers,
                        score = excluded.score,
                        updated_at = excluded.updated_at
                    """,
                    (
                        user["id"],
                        user.get("name", "Unknown"),
                        1 if self.is_supreme_admin(user["id"]) else 0,
                        user.get("quiz_played", 0),
                        user.get("correct", 0),
                        user.get("wrong", 0),
                        user.get("score", 0),
                        timestamp,
                        timestamp,
                    ),
                )

    def can_generate_free_pdf(self, user: dict) -> bool:
        return int(user.get("pdf_generation_count", 0)) < 1

    def record_pdf_generation(self, user_id: int):
        with database.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET pdf_generation_count = pdf_generation_count + 1,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (now_iso(), user_id),
            )

    def set_premium_expiry(self, user_id: int, expiry_iso: str | None, is_premium: bool):
        normalized_expiry = expiry_iso
        if expiry_iso:
            parsed_expiry = parse_utc_datetime(expiry_iso)
            normalized_expiry = parsed_expiry.replace(microsecond=0).isoformat() if parsed_expiry else expiry_iso
        with database.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET is_premium = ?, premium_expires_at = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (1 if is_premium else 0, normalized_expiry, now_iso(), user_id),
            )
        return self.get_user(user_id)

    def _normalize_premium_status(self, user: dict) -> dict:
        if not user:
            return {}

        user_id = user.get("user_id")
        if user_id is None:
            logger.warning("Premium normalization skipped | reason=user_id_missing user=%s", user)
            return user

        expiry = user.get("premium_expires_at")
        if not expiry:
            return user

        expiry_dt = parse_utc_datetime(expiry)
        if not expiry_dt:
            logger.warning(
                "Premium normalization skipped | user_id=%s reason=invalid_expiry_format premium_expires_at=%s",
                user_id,
                expiry,
            )
            return user

        if expiry_dt > datetime.now(timezone.utc):
            return user

        if user.get("is_premium"):
            try:
                with database.connection() as conn:
                    conn.execute(
                        """
                        UPDATE users
                        SET is_premium = 0, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (now_iso(), user_id),
                    )
                user["is_premium"] = 0
                logger.info("Premium normalized | user_id=%s action=expired_premium_downgraded expiry=%s", user_id, expiry)
            except Exception:
                logger.exception(
                    "Premium normalization failed | user_id=%s sql=expired_premium_update expiry=%s",
                    user_id,
                    expiry,
                )

        return user

    def _users_table_has_integer_primary_key(self, conn) -> bool:
        return database.users_table_has_integer_primary_key(conn)

    def _normalize_login_identifier(self, value: str | None) -> str:
        raw = (value or "").strip()
        if "@" in raw:
            return self._normalize_email(raw)
        return self._normalize_phone(raw) or raw.lower()

    def _coerce_user_id(self, value) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _resolve_linked_website_user_id(self, user_id: int) -> int:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT website_user_id FROM telegram_account_links WHERE telegram_user_id = ?",
                (int(user_id),),
            ).fetchone()
        return int(row["website_user_id"]) if row and row["website_user_id"] is not None else int(user_id)

    def _normalize_email(self, value: str | None) -> str:
        normalized = (value or "").strip().lower()
        return normalized if normalized and "@" in normalized else ""

    def _normalize_phone(self, value: str | None) -> str:
        raw = (value or "").strip()
        digits = "".join(character for character in raw if character.isdigit())
        if len(digits) < 10:
            return ""
        return digits

    def _clean_full_name(self, value: str | None) -> str:
        return " ".join((value or "").strip().split())

    def _normalize_user_dict(self, user: dict) -> dict:
        if not user:
            return {}

        user_id = int(user.get("user_id", 0) or 0)
        website_name = self._clean_full_name(user.get("website_name")) or self._clean_full_name(user.get("full_name"))
        telegram_full_name = self._clean_full_name(user.get("telegram_full_name"))
        telegram_first_name = self._clean_full_name(user.get("telegram_first_name"))
        telegram_username = (user.get("telegram_username") or user.get("username") or "").strip() or None
        if not telegram_full_name and user.get("username"):
            telegram_full_name = self._clean_full_name(user.get("full_name"))
        if not telegram_first_name and telegram_full_name:
            telegram_first_name = telegram_full_name.split(" ", 1)[0]
        user["website_name"] = website_name or "QuizPathshala User"
        user["telegram_full_name"] = telegram_full_name or user["website_name"]
        user["telegram_first_name"] = telegram_first_name or user["telegram_full_name"].split(" ", 1)[0]
        user["telegram_username"] = telegram_username
        user["full_name"] = user["website_name"]
        user["username"] = user.get("username") or telegram_username
        role = user.get("user_role") or ("admin" if user.get("is_admin") else "user")
        if role == "super_admin":
            user["is_admin"] = 1
        elif self.is_admin(user_id):
            user["is_admin"] = 1
            role = "admin"
        user["user_role"] = role
        return user

    def website_display_name(self, user: dict) -> str:
        return self._clean_full_name(user.get("website_name") or user.get("full_name")) or "QuizPathshala User"

    def telegram_display_name(self, user: dict) -> str:
        return (
            self._clean_full_name(user.get("telegram_full_name"))
            or self._clean_full_name(user.get("telegram_first_name"))
            or self._clean_full_name(user.get("full_name"))
            or (user.get("telegram_username") or user.get("username") or "").strip()
            or "Telegram User"
        )

    def telegram_handle(self, user: dict) -> str | None:
        raw = (user.get("telegram_username") or user.get("username") or "").strip()
        return raw or None

    def _generate_persistent_user_id(self) -> int:
        with database.connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(user_id), 7000000000) AS max_id FROM users").fetchone()
        return int(row["max_id"]) + 1

    def reset_daily_counter_if_needed(self, user: dict) -> dict:
        today = date.today().isoformat()

        if user.get("daily_question_date") == today:
            return user

        with database.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET daily_question_date = ?, daily_question_count = 0, updated_at = ?
                WHERE user_id = ?
                """,
                (today, now_iso(), user["user_id"]),
            )

        return self.get_user(user["user_id"])

    def increment_daily_questions(self, user_id: int, count: int):
        user = self.reset_daily_counter_if_needed(self.get_user(user_id))
        today = date.today().isoformat()

        with database.connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET daily_question_date = ?,
                    daily_question_count = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (today, user["daily_question_count"] + count, now_iso(), user_id),
            )


user_service = UserService()
