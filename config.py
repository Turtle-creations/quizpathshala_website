import os
from pathlib import Path


def _read_bool_env(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_database_url(raw_value: str) -> str:
    normalized = (raw_value or "").strip()
    if normalized.startswith("postgres://"):
        return "postgresql://" + normalized[len("postgres://"):]
    return normalized


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
IMAGE_DIR = DATA_DIR / "images"
APP_ENV = (os.getenv("APP_ENV") or os.getenv("FLASK_ENV") or "development").strip().lower()
DATABASE_URL = _normalize_database_url(os.getenv("DATABASE_URL") or "")
_db_path_env = (os.getenv("DB_PATH") or "").strip()
_legacy_shared_database_env = (os.getenv("QUIZPATHSHALA_SHARED_DB") or "").strip()
_legacy_shared_data_dir_env = (os.getenv("QUIZPATHSHALA_SHARED_DATA_DIR") or "").strip()
_sibling_bot_database = (BASE_DIR.parent / "quiz_bot" / "data" / "quiz_bot_v2.db").resolve()

if DATABASE_URL:
    DATABASE_BACKEND = "postgres"
    DATABASE_PATH = None
    DATABASE_DSN = DATABASE_URL
else:
    DATABASE_BACKEND = "sqlite"
    if _db_path_env:
        DATABASE_PATH = Path(_db_path_env).expanduser().resolve()
    elif _legacy_shared_database_env:
        DATABASE_PATH = Path(_legacy_shared_database_env).expanduser().resolve()
    elif _legacy_shared_data_dir_env:
        DATABASE_PATH = Path(_legacy_shared_data_dir_env).expanduser().resolve() / "quiz_bot_v2.db"
    elif APP_ENV != "production" and _sibling_bot_database.exists():
        DATABASE_PATH = _sibling_bot_database
    else:
        DATABASE_PATH = DATA_DIR / "quiz_bot_v2.db"
    DATABASE_DSN = str(DATABASE_PATH)
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
SECRET_KEY = (os.getenv("SECRET_KEY") or "quizpathshala-web-secret").strip()
PORT = int(os.getenv("PORT", "10000"))
DEFAULT_QUESTION_TIME = int(os.getenv("DEFAULT_QUESTION_TIME", "15"))
FREE_DAILY_QUESTION_LIMIT = int(os.getenv("FREE_DAILY_QUESTION_LIMIT", "10"))

SITE_NAME = "QuizPathshala"
SITE_TAGLINE = "Online quiz preparation platform via Telegram bot"
BOT_URL = os.getenv("BOT_URL", "https://t.me/QuizPathshala_bot")
BOT_USERNAME = os.getenv("BOT_USERNAME", "QuizPathshala_bot")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "")
SUPPORT_TELEGRAM = os.getenv("SUPPORT_TELEGRAM", "https://t.me/QuizPathshala_bot")
SUPPORT_HOURS = os.getenv("SUPPORT_HOURS", "Monday to Saturday, 10:00 AM to 7:00 PM IST")
CANONICAL_URL = os.getenv("CANONICAL_URL", "").rstrip("/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", CANONICAL_URL or "")

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_LOGIN_IDENTIFIER = os.getenv("ADMIN_LOGIN_IDENTIFIER", "admin")
SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "kr.amitsoren@gmail.com").strip().lower()
SUPER_ADMIN_PASSWORD = os.getenv("SUPER_ADMIN_PASSWORD", "")
SUPER_ADMIN_NAME = os.getenv("SUPER_ADMIN_NAME", "QuizPathshala Super Admin")
DEMO_USER_LOGIN = os.getenv("DEMO_USER_LOGIN", "")
DEMO_USER_PASSWORD = os.getenv("DEMO_USER_PASSWORD", "")
DEMO_USER_NAME = os.getenv("DEMO_USER_NAME", "QuizPathshala Student")
SUPREME_ADMIN_ID = int(os.getenv("SUPREME_ADMIN_ID", "1341448466"))
ADMINS = {
    int(item.strip())
    for item in os.getenv("ADMINS", "").split(",")
    if item.strip().isdigit()
}

PASSWORD_RESET_LOCAL_DEV_OTP = _read_bool_env("PASSWORD_RESET_LOCAL_DEV_OTP", APP_ENV != "production")
PASSWORD_RESET_OTP_TTL_MINUTES = int(os.getenv("PASSWORD_RESET_OTP_TTL_MINUTES", "10"))
SMTP_HOST = (os.getenv("SMTP_HOST") or "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = (os.getenv("SMTP_USERNAME") or "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = (os.getenv("SMTP_FROM_EMAIL") or SUPPORT_EMAIL or SMTP_USERNAME).strip()
SMTP_USE_TLS = _read_bool_env("SMTP_USE_TLS", True)
