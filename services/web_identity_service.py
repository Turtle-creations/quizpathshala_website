import random

from flask import g, has_request_context, session

from config import ADMIN_LOGIN_IDENTIFIER
from services.user_service_db import user_service


class WebIdentityService:
    SESSION_KEY = "web_user_id"
    AUTH_USER_KEY = "auth_user_id"
    ROLE_KEY = "auth_role"
    ADMIN_KEY = "admin_authenticated"
    SNAPSHOT_KEY = "auth_user_snapshot"
    _AUTH_CACHE_KEY = "_web_identity_authenticated_user"
    _SNAPSHOT_CACHE_KEY = "_web_identity_authenticated_snapshot"
    _CACHE_MISS = object()
    _FULL_SNAPSHOT_FIELDS = {
        "user_id",
        "full_name",
        "username",
        "email",
        "phone_number",
        "user_role",
        "is_admin",
        "is_premium",
        "premium_expires_at",
        "score",
        "created_at",
    }

    def get_or_create_user(self) -> dict:
        authenticated = self.get_authenticated_user()
        if authenticated:
            session[self.SESSION_KEY] = authenticated["user_id"]
            session["web_user_name"] = authenticated.get("full_name") or "QuizPathshala User"
            return authenticated

        user_id = session.get(self.SESSION_KEY)
        if not user_id:
            user_id = self._generate_user_id()
            session[self.SESSION_KEY] = user_id

        full_name = session.get("web_user_name") or f"Guest {str(user_id)[-6:]}"
        session["web_user_name"] = full_name
        return user_service.ensure_profile(
            user_id=int(user_id),
            full_name=full_name,
            username=None,
            is_admin=self.is_admin_authenticated(),
        )

    def update_name(self, full_name: str) -> dict:
        cleaned = (full_name or "").strip() or "Guest User"
        session["web_user_name"] = cleaned
        current = self.get_or_create_user()
        return user_service.ensure_profile(
            user_id=current["user_id"],
            full_name=cleaned,
            username=current.get("username"),
            is_admin=self._is_privileged_role(self.get_role()),
        )

    def set_authenticated_user(self, user: dict) -> None:
        session[self.AUTH_USER_KEY] = int(user["user_id"])
        normalized_role = str(user.get("user_role") or ("admin" if user.get("is_admin") else "user"))
        session[self.ROLE_KEY] = normalized_role
        session[self.SESSION_KEY] = int(user["user_id"])
        session["web_user_name"] = user.get("full_name") or "QuizPathshala User"
        if self._is_privileged_role(normalized_role) or user.get("is_admin"):
            session[self.ADMIN_KEY] = True
        else:
            session.pop(self.ADMIN_KEY, None)
        snapshot = self._build_user_snapshot(user)
        session[self.SNAPSHOT_KEY] = snapshot
        self._set_cached_authenticated_user(dict(user))
        self._set_cached_authenticated_snapshot(snapshot)

    def authenticate(self, login_identifier: str, password: str) -> dict:
        return user_service.authenticate_web_user(login_identifier, password)

    def register(self, full_name: str, email: str, phone_number: str, password: str) -> tuple[dict, str | None]:
        return user_service.register_web_user(
            full_name=full_name,
            email=email,
            phone_number=phone_number,
            password=password,
        )

    def get_authenticated_user(self) -> dict:
        user_id = session.get(self.AUTH_USER_KEY)
        if not user_id:
            self._set_cached_authenticated_user({})
            self._set_cached_authenticated_snapshot({})
            return {}

        cached_user = self._get_cached_authenticated_user()
        if cached_user is not self._CACHE_MISS:
            return dict(cached_user) if cached_user else {}

        snapshot = self.get_authenticated_user_snapshot()
        if self._snapshot_can_serve_authenticated_user(snapshot, user_id):
            self._set_cached_authenticated_user(snapshot)
            return dict(snapshot)

        user = user_service.get_user(int(user_id))
        if not user:
            self.logout_user()
            return {}

        snapshot = self._build_user_snapshot(user)
        session[self.SNAPSHOT_KEY] = snapshot
        self._set_cached_authenticated_user(dict(user))
        self._set_cached_authenticated_snapshot(snapshot)
        return dict(user)

    def is_authenticated(self) -> bool:
        return bool(session.get(self.AUTH_USER_KEY))

    def get_role(self) -> str:
        snapshot = self.get_authenticated_user_snapshot()
        if snapshot:
            return str(snapshot.get("user_role") or ("admin" if snapshot.get("is_admin") else "user"))
        return str(session.get(self.ROLE_KEY) or "")

    def mark_admin_authenticated(self) -> None:
        admin_user = user_service.find_by_login_identifier(ADMIN_LOGIN_IDENTIFIER)
        if admin_user:
            self.set_authenticated_user(admin_user)
        else:
            session[self.ADMIN_KEY] = True

    def logout_user(self) -> None:
        self._clear_cached_authenticated_user()
        session.pop(self.AUTH_USER_KEY, None)
        session.pop(self.ROLE_KEY, None)
        session.pop(self.ADMIN_KEY, None)
        session.pop(self.SNAPSHOT_KEY, None)
        session.pop(self.SESSION_KEY, None)
        session.pop("web_user_name", None)

    def clear_admin_authenticated(self) -> None:
        self.logout_user()

    def is_admin_authenticated(self) -> bool:
        snapshot = self.get_authenticated_user_snapshot()
        role = snapshot.get("user_role") if snapshot else session.get(self.ROLE_KEY)
        return bool(snapshot and (self._is_privileged_role(role) or snapshot.get("is_admin"))) or bool(session.get(self.ADMIN_KEY))

    def get_authenticated_user_snapshot(self) -> dict:
        cached_snapshot = self._get_cached_authenticated_snapshot()
        if cached_snapshot is not self._CACHE_MISS:
            return dict(cached_snapshot) if cached_snapshot else {}

        snapshot = session.get(self.SNAPSHOT_KEY) or {}
        self._set_cached_authenticated_snapshot(snapshot)
        return dict(snapshot) if snapshot else {}

    def get_authenticated_user_id(self) -> int | None:
        user_id = session.get(self.AUTH_USER_KEY)
        return int(user_id) if user_id is not None else None

    def _get_cached_authenticated_user(self):
        if not has_request_context():
            return self._CACHE_MISS
        return getattr(g, self._AUTH_CACHE_KEY, self._CACHE_MISS)

    def _set_cached_authenticated_user(self, user: dict) -> None:
        if has_request_context():
            setattr(g, self._AUTH_CACHE_KEY, dict(user) if user else {})

    def _get_cached_authenticated_snapshot(self):
        if not has_request_context():
            return self._CACHE_MISS
        return getattr(g, self._SNAPSHOT_CACHE_KEY, self._CACHE_MISS)

    def _set_cached_authenticated_snapshot(self, user: dict) -> None:
        if has_request_context():
            setattr(g, self._SNAPSHOT_CACHE_KEY, dict(user) if user else {})

    def _clear_cached_authenticated_user(self) -> None:
        if has_request_context() and hasattr(g, self._AUTH_CACHE_KEY):
            delattr(g, self._AUTH_CACHE_KEY)
        if has_request_context() and hasattr(g, self._SNAPSHOT_CACHE_KEY):
            delattr(g, self._SNAPSHOT_CACHE_KEY)

    def _build_user_snapshot(self, user: dict) -> dict:
        return {
            "user_id": int(user["user_id"]),
            "full_name": user.get("full_name") or "QuizPathshala User",
            "username": user.get("username"),
            "email": user.get("email"),
            "phone_number": user.get("phone_number"),
            "user_role": str(user.get("user_role") or ("admin" if user.get("is_admin") else "user")),
            "is_admin": 1 if user.get("is_admin") else 0,
            "is_premium": 1 if user.get("is_premium") else 0,
            "premium_expires_at": user.get("premium_expires_at"),
            "score": float(user.get("score") or 0),
            "created_at": user.get("created_at"),
        }

    def _snapshot_can_serve_authenticated_user(self, snapshot: dict, user_id) -> bool:
        if not snapshot:
            return False
        try:
            snapshot_user_id = int(snapshot.get("user_id"))
            expected_user_id = int(user_id)
        except (TypeError, ValueError):
            return False
        if snapshot_user_id != expected_user_id:
            return False
        return self._FULL_SNAPSHOT_FIELDS.issubset(snapshot.keys())

    def _generate_user_id(self) -> int:
        return random.randint(7000000000, 7999999999)

    def _is_privileged_role(self, role: str | None) -> bool:
        return str(role or "") in {"admin", "super_admin"}


web_identity_service = WebIdentityService()
