from datetime import UTC, datetime
from functools import wraps
import hmac
import uuid

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from config import ADMIN_PASSWORD, SECRET_KEY, SUPPORT_TELEGRAM
from db.database import database
from services.payment_service_db import payment_service
from services.web_admin_service import web_admin_service
from services.web_identity_service import web_identity_service
from utils.logging_utils import get_logger


admin_blueprint = Blueprint("admin", __name__)
logger = get_logger(__name__)
_MOJIBAKE_MARKERS = ("\u00e0\u00a4", "\u00e0\u00a5")
_MOJIBAKE_FIELDS = (
    "question_text",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_option",
    "explanation",
)
_BACKUP_TABLE = "mojibake_question_backups"


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not web_identity_service.is_admin_authenticated():
            flash("Please log in as admin first.", "error")
            return redirect(url_for("auth.login"))
        return view_func(*args, **kwargs)

    return wrapper


def _is_super_admin(user: dict | None) -> bool:
    return bool(user and str(user.get("user_role") or "") == "super_admin")


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _contains_mojibake(value) -> bool:
    return isinstance(value, str) and any(marker in value for marker in _MOJIBAKE_MARKERS)


def _repair_mojibake_text(value: str) -> str:
    return value.encode("latin1").decode("utf-8")


def _ensure_mojibake_backup_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_BACKUP_TABLE} (
            backup_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            question_id BIGINT NOT NULL,
            question_text TEXT,
            option_a TEXT,
            option_b TEXT,
            option_c TEXT,
            option_d TEXT,
            correct_option TEXT,
            explanation TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_BACKUP_TABLE}_batch_id ON {_BACKUP_TABLE}(batch_id)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_BACKUP_TABLE}_question_id ON {_BACKUP_TABLE}(question_id)"
    )


def _fetch_affected_question_rows(conn) -> list[dict]:
    where_clauses = []
    params: list[str] = []
    for field_name in _MOJIBAKE_FIELDS:
        for marker in _MOJIBAKE_MARKERS:
            where_clauses.append(f"{field_name} LIKE ?")
            params.append(f"%{marker}%")

    query = (
        "SELECT question_id, question_text, option_a, option_b, option_c, option_d, correct_option, explanation "
        "FROM questions WHERE " + " OR ".join(where_clauses) + " ORDER BY question_id"
    )
    return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def _backup_question_rows(conn, *, batch_id: str, rows: list[dict]) -> int:
    created_at = _utc_now_iso()
    backup_count = 0
    for row in rows:
        conn.execute(
            f"""
            INSERT INTO {_BACKUP_TABLE} (
                backup_id, batch_id, question_id, question_text, option_a, option_b,
                option_c, option_d, correct_option, explanation, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                batch_id,
                row["question_id"],
                row.get("question_text"),
                row.get("option_a"),
                row.get("option_b"),
                row.get("option_c"),
                row.get("option_d"),
                row.get("correct_option"),
                row.get("explanation"),
                created_at,
            ),
        )
        backup_count += 1
    return backup_count


def _build_question_changes(row: dict) -> dict[str, str]:
    changes: dict[str, str] = {}
    for field_name in _MOJIBAKE_FIELDS:
        value = row.get(field_name)
        if not _contains_mojibake(value):
            continue
        try:
            repaired = _repair_mojibake_text(value)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != value:
            changes[field_name] = repaired
    return changes


def _apply_mojibake_repairs(conn, rows: list[dict]) -> tuple[int, int]:
    updated_rows = 0
    updated_fields = 0
    for row in rows:
        changes = _build_question_changes(row)
        if not changes:
            continue

        assignments = ", ".join(f"{field_name} = ?" for field_name in changes)
        params = [changes[field_name] for field_name in changes]
        params.append(row["question_id"])
        conn.execute(
            f"UPDATE questions SET {assignments} WHERE question_id = ?",
            tuple(params),
        )
        updated_rows += 1
        updated_fields += len(changes)

    return updated_rows, updated_fields


@admin_blueprint.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    current_user = web_identity_service.get_authenticated_user()
    if current_user and web_identity_service.is_admin_authenticated():
        return redirect(url_for("admin.admin_dashboard"))

    web_identity_service.get_or_create_user()
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            web_identity_service.mark_admin_authenticated()
            flash("Admin access granted.", "success")
            return redirect(url_for("admin.admin_dashboard"))
        flash("Invalid admin password.", "error")
    return render_template(
        "admin_login.html",
        page_title="Admin Login",
        support_telegram=SUPPORT_TELEGRAM,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@admin_blueprint.route("/admin/logout", methods=["POST"])
def admin_logout():
    web_identity_service.clear_admin_authenticated()
    flash("Admin session closed.", "success")
    return redirect(url_for("pages.home"))


@admin_blueprint.route("/admin/repair-mojibake", methods=["GET"])
def repair_mojibake():
    token = request.args.get("token", "")
    if not token or not hmac.compare_digest(token, SECRET_KEY):
        logger.warning(
            "Mojibake repair denied | remote_addr=%s reason=invalid_token",
            request.headers.get("X-Forwarded-For", request.remote_addr),
        )
        return jsonify({"ok": False, "error": "forbidden"}), 403

    batch_id = f"mojibake-repair-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    logger.info("Mojibake repair started | batch_id=%s", batch_id)

    try:
        with database.connection() as conn:
            _ensure_mojibake_backup_table(conn)
            affected_rows = _fetch_affected_question_rows(conn)
            backup_rows = _backup_question_rows(conn, batch_id=batch_id, rows=affected_rows)
            fixed_rows, fixed_fields = _apply_mojibake_repairs(conn, affected_rows)

        logger.info(
            "Mojibake repair completed | batch_id=%s affected_rows=%s backup_rows=%s fixed_rows=%s fixed_fields=%s",
            batch_id,
            len(affected_rows),
            backup_rows,
            fixed_rows,
            fixed_fields,
        )
        return jsonify(
            {
                "ok": True,
                "batch_id": batch_id,
                "affected_rows": len(affected_rows),
                "backup_rows": backup_rows,
                "fixed_rows": fixed_rows,
                "fixed_fields": fixed_fields,
                "backup_table": _BACKUP_TABLE,
            }
        )
    except Exception as exc:
        logger.exception("Mojibake repair failed | batch_id=%s", batch_id)
        return jsonify({"ok": False, "batch_id": batch_id, "error": str(exc)}), 500


@admin_blueprint.route("/admin", methods=["GET", "POST"])
@admin_required
def admin_dashboard():
    current_user = web_identity_service.get_authenticated_user()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        try:
            if action == "update_price":
                payment_service.update_premium_price(
                    request.form.get("plan_type", ""),
                    request.form.get("amount", ""),
                )
                flash("Premium price updated successfully.", "success")
            elif action == "add_exam":
                web_admin_service.add_exam(
                    request.form.get("title", ""),
                    request.form.get("description") or None,
                )
                flash("Exam added successfully.", "success")
            elif action == "add_set":
                web_admin_service.add_set(
                    exam_id=int(request.form.get("exam_id", "0")),
                    title=request.form.get("title", ""),
                    description=request.form.get("description") or None,
                    is_premium_locked=bool(request.form.get("is_premium_locked")),
                )
                flash("Set added successfully.", "success")
            elif action == "add_question":
                web_admin_service.add_question(
                    exam_id=int(request.form.get("exam_id", "0")),
                    set_id=int(request.form.get("set_id", "0")),
                    question_text=request.form.get("question_text", ""),
                    options=[
                        request.form.get("option_a", ""),
                        request.form.get("option_b", ""),
                        request.form.get("option_c", ""),
                        request.form.get("option_d", ""),
                    ],
                    correct_option=request.form.get("correct_option", ""),
                    explanation=request.form.get("explanation") or None,
                    image_path=request.form.get("image_path") or None,
                    time_limit=int(request.form.get("time_limit", "0") or "0") or None,
                )
                flash("Question added successfully.", "success")
            elif action == "bulk_import":
                created = web_admin_service.bulk_import_questions(
                    exam_id=int(request.form.get("exam_id", "0")),
                    set_id=int(request.form.get("set_id", "0")),
                    raw_text=request.form.get("bulk_payload", ""),
                )
                flash(f"Bulk import completed: {len(created)} questions added.", "success")
            elif action == "toggle_set_lock":
                web_admin_service.set_set_lock(
                    set_id=int(request.form.get("set_id", "0")),
                    is_locked=request.form.get("lock_state", "0") == "1",
                )
                flash("Set access updated successfully.", "success")
            elif action == "delete_question":
                deleted = web_admin_service.delete_question(int(request.form.get("question_id", "0")))
                flash("Question deleted successfully." if deleted else "Question not found.", "success" if deleted else "error")
            elif action == "delete_set":
                web_admin_service.delete_set(int(request.form.get("set_id", "0")))
                flash("Set deleted successfully.", "success")
            elif action == "delete_exam":
                web_admin_service.delete_exam(int(request.form.get("exam_id", "0")))
                flash("Exam deleted successfully.", "success")
            elif action == "change_role":
                if not _is_super_admin(current_user):
                    raise ValueError("Only the super admin can change website roles.")
                updated_user, status = web_admin_service.change_user_role(
                    int(request.form.get("target_user_id", "0")),
                    request.form.get("target_role", ""),
                )
                if not updated_user and status == "not_found":
                    raise ValueError("Target user not found.")
                flash("User role updated successfully.", "success")
            else:
                flash("Unknown admin action.", "error")
        except Exception as exc:
            flash(str(exc), "error")
        return redirect(url_for("admin.admin_dashboard", q=request.args.get("q", "")))

    dashboard = web_admin_service.dashboard_data()
    dashboard.pop("question_search_results", None)
    search_query = (request.args.get("q") or "").strip()
    question_search_results = web_admin_service.search_questions(search_query) if search_query else []
    catalog = web_admin_service.catalog_for_admin()
    return render_template(
        "admin_dashboard.html",
        page_title="Admin Panel",
        support_telegram=SUPPORT_TELEGRAM,
        admin_authenticated=True,
        current_user=current_user,
        search_query=search_query,
        question_search_results=question_search_results,
        catalog=catalog,
        **dashboard,
    )
