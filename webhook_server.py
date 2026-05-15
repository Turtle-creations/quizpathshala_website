from flask import Flask, abort, request, send_file

from config import (
    APP_ENV,
    BASE_DIR,
    BOT_URL,
    CANONICAL_URL,
    DATABASE_BACKEND,
    DATABASE_HOST,
    DATABASE_PORT,
    PORT,
    SECRET_KEY,
    SITE_NAME,
    SITE_TAGLINE,
    STATIC_DIR,
    SUPPORT_HOURS,
    SUPPORT_TELEGRAM,
    TEMPLATES_DIR,
)
from db.database import database
from routes.admin import admin_blueprint
from routes.auth import auth_blueprint
from routes.pages import pages_blueprint
from routes.premium import premium_blueprint
from routes.quiz import quiz_blueprint
from services.bootstrap_service import bootstrap_application
from services.web_identity_service import web_identity_service
from services.web_password_reset_service import web_password_reset_service
from utils.logging_utils import get_logger, setup_logging


setup_logging()
logger = get_logger(__name__)
MEDIA_ROOT = (BASE_DIR / "data").resolve()


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(TEMPLATES_DIR),
        static_folder=str(STATIC_DIR),
        static_url_path="/static",
    )
    app.config["SECRET_KEY"] = SECRET_KEY
    app.config["JSON_AS_ASCII"] = False
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400
    app.json.ensure_ascii = False

    if APP_ENV == "production" and SECRET_KEY == "quizpathshala-web-secret":
        logger.warning("Production is using the default SECRET_KEY. Set SECRET_KEY in Render environment variables.")

    logger.info(
        "Initializing QuizPathshala app | env=%s database_backend=%s database_host=%s database_port=%s",
        APP_ENV,
        DATABASE_BACKEND,
        DATABASE_HOST or "sqlite-local",
        DATABASE_PORT or "-",
    )
    web_password_reset_service.log_smtp_configuration_status()

    try:
        database.initialize()
        bootstrap_application()
        logger.info("Database initialization complete")
    except Exception:
        logger.exception("Application startup failed during database initialization/bootstrap")
        raise

    @app.after_request
    def ensure_utf8_response(response):
        if response.mimetype in {"text/html", "application/json", "text/plain", "application/javascript", "text/javascript"}:
            response.headers["Content-Type"] = f"{response.mimetype}; charset=utf-8"

        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif request.path.startswith("/media/"):
            response.headers["Cache-Control"] = "public, max-age=3600"

        return response

    @app.context_processor
    def inject_site_context():
        current_user = web_identity_service.get_authenticated_user_snapshot()
        current_role = current_user.get("user_role") if current_user else ""
        admin_authenticated = bool(
            current_user and (current_role in {"admin", "super_admin"} or current_user.get("is_admin"))
        ) or web_identity_service.is_admin_authenticated()
        return {
            "site_name": SITE_NAME,
            "tagline": SITE_TAGLINE,
            "bot_url": BOT_URL,
            "support_hours": SUPPORT_HOURS,
            "support_telegram": SUPPORT_TELEGRAM,
            "canonical_url": CANONICAL_URL,
            "current_user": current_user,
            "is_authenticated": bool(current_user),
            "current_role": current_role,
            "admin_authenticated": admin_authenticated,
        }

    app.register_blueprint(auth_blueprint)
    app.register_blueprint(pages_blueprint)
    app.register_blueprint(quiz_blueprint)
    app.register_blueprint(premium_blueprint)
    app.register_blueprint(admin_blueprint)

    @app.route("/media/<path:relative_path>")
    def media_file(relative_path: str):
        requested = (BASE_DIR / relative_path).resolve()
        if not requested.is_file():
            abort(404)
        if MEDIA_ROOT not in requested.parents:
            abort(404)
        return send_file(requested, conditional=True, max_age=3600)

    return app


app = create_app()


if __name__ == "__main__":
    logger.info("Starting QuizPathshala website on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
