import json

from config import DATA_DIR
from services.exam_service_db import exam_service
from services.user_service_db import user_service


_BOOTSTRAP_COMPLETED = False


def bootstrap_application():
    global _BOOTSTRAP_COMPLETED

    if _BOOTSTRAP_COMPLETED:
        return

    exam_service.import_legacy_data()
    exam_service.migrate_correct_answers_to_text()
    _import_legacy_users()
    user_service.ensure_default_web_accounts()
    _BOOTSTRAP_COMPLETED = True


def _import_legacy_users():
    users_file = DATA_DIR / "users.json"
    if not users_file.exists():
        return

    try:
        users = json.loads(users_file.read_text(encoding="utf-8"))
    except Exception:
        return

    if isinstance(users, list):
        user_service.sync_json_user_stats(users)
