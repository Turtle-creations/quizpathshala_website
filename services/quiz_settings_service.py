from db.database import database
from services.user_service_db import now_iso


class QuizSettingsService:
    DEFAULT_ALLOW_RESUME = True
    DEFAULT_MAX_ATTEMPTS_PER_SET = 0
    DEFAULT_MAX_BREAKS = 4

    KEY_ALLOW_RESUME = "quiz:allow_resume"
    KEY_MAX_ATTEMPTS_PER_SET = "quiz:max_attempts_per_set"
    KEY_MAX_BREAKS = "quiz:max_breaks"

    def get_settings(self) -> dict:
        keys = (
            self.KEY_ALLOW_RESUME,
            self.KEY_MAX_ATTEMPTS_PER_SET,
            self.KEY_MAX_BREAKS,
        )
        with database.connection() as conn:
            rows = conn.execute(
                f"SELECT key, value FROM settings WHERE key IN ({', '.join('?' for _ in keys)})",
                keys,
            ).fetchall()

        stored = {str(row["key"]): str(row["value"]) for row in rows}
        return {
            "allow_resume": self._coerce_bool(stored.get(self.KEY_ALLOW_RESUME), self.DEFAULT_ALLOW_RESUME),
            "max_attempts_per_set": self._coerce_int(
                stored.get(self.KEY_MAX_ATTEMPTS_PER_SET),
                self.DEFAULT_MAX_ATTEMPTS_PER_SET,
                minimum=0,
            ),
            "max_breaks": self._coerce_int(
                stored.get(self.KEY_MAX_BREAKS),
                self.DEFAULT_MAX_BREAKS,
                minimum=0,
            ),
        }

    def update_settings(
        self,
        *,
        allow_resume: bool,
        max_attempts_per_set: int,
        max_breaks: int,
    ) -> dict:
        cleaned_attempts = max(int(max_attempts_per_set), 0)
        cleaned_breaks = max(int(max_breaks), 0)
        pairs = (
            (self.KEY_ALLOW_RESUME, "1" if allow_resume else "0"),
            (self.KEY_MAX_ATTEMPTS_PER_SET, str(cleaned_attempts)),
            (self.KEY_MAX_BREAKS, str(cleaned_breaks)),
        )
        with database.connection() as conn:
            for key, value in pairs:
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
        return self.get_settings()

    def _coerce_bool(self, value: str | None, default: bool) -> bool:
        if value is None:
            return default
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    def _coerce_int(self, value: str | None, default: int, *, minimum: int = 0) -> int:
        try:
            parsed = int(str(value).strip()) if value is not None else default
        except (TypeError, ValueError):
            return default
        return parsed if parsed >= minimum else default


quiz_settings_service = QuizSettingsService()
