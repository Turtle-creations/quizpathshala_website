import json

from db.database import database
from services.user_service_db import now_iso
from utils.logging_utils import get_logger


logger = get_logger(__name__)


class UserActivityService:
    def record(
        self,
        *,
        user_id: int | None,
        action: str,
        identifier: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        details: dict | str | None = None,
    ) -> None:
        if not user_id or not action:
            return

        try:
            details_text = self._serialize_details(details)
            with database.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO user_activity_logs (
                        user_id,
                        action,
                        identifier,
                        ip_address,
                        user_agent,
                        details,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(user_id),
                        str(action).strip(),
                        (identifier or "").strip() or None,
                        (ip_address or "").strip() or None,
                        (user_agent or "").strip() or None,
                        details_text,
                        now_iso(),
                    ),
                )
        except Exception:
            logger.exception(
                "User activity logging failed | user_id=%s action=%s identifier=%s",
                user_id,
                action,
                identifier,
            )

    def _serialize_details(self, details: dict | str | None) -> str | None:
        if details is None:
            return None
        if isinstance(details, str):
            return details.strip() or None
        try:
            return json.dumps(details, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(details)


user_activity_service = UserActivityService()
