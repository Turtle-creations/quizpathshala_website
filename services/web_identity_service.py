import random

from flask import session

from config import ADMIN_LOGIN_IDENTIFIER
from services.user_service_db import user_service


class WebIdentityService:
    SESSION_KEY = "web_user_id"
    AUTH_USER_KEY = "auth_user_id"
    ROLE_KEY = "auth_role"
    ADMIN_KEY = "admin_authenticated"

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
            return {}

        user = user_service.get_user(int(user_id))
        if not user:
            self.logout_user()
            return {}
        return user

    def is_authenticated(self) -> bool:
        return bool(self.get_authenticated_user())

    def get_role(self) -> str:
        user = self.get_authenticated_user()
        if user:
            return str(user.get("user_role") or ("admin" if user.get("is_admin") else "user"))
        return str(session.get(self.ROLE_KEY) or "")

    def mark_admin_authenticated(self) -> None:
        admin_user = user_service.find_by_login_identifier(ADMIN_LOGIN_IDENTIFIER)
        if admin_user:
            self.set_authenticated_user(admin_user)
        else:
            session[self.ADMIN_KEY] = True

    def logout_user(self) -> None:
        session.pop(self.AUTH_USER_KEY, None)
        session.pop(self.ROLE_KEY, None)
        session.pop(self.ADMIN_KEY, None)
        session.pop(self.SESSION_KEY, None)
        session.pop("web_user_name", None)

    def clear_admin_authenticated(self) -> None:
        self.logout_user()

    def is_admin_authenticated(self) -> bool:
        user = self.get_authenticated_user()
        role = user.get("user_role") if user else session.get(self.ROLE_KEY)
        return bool(user and (self._is_privileged_role(role) or user.get("is_admin"))) or bool(session.get(self.ADMIN_KEY))

    def _generate_user_id(self) -> int:
        return random.randint(7000000000, 7999999999)

    def _is_privileged_role(self, role: str | None) -> bool:
        return str(role or "") in {"admin", "super_admin"}


web_identity_service = WebIdentityService()
