from datetime import UTC, datetime
from functools import wraps
import hmac
import re
import uuid

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from config import ADMIN_PASSWORD, SECRET_KEY, SUPPORT_TELEGRAM
from db.database import database
from services.exam_service_db import exam_service
from services.payment_service_db import payment_service
from services.web_admin_service import web_admin_service
from services.web_identity_service import web_identity_service
from utils.logging_utils import get_logger


admin_blueprint = Blueprint("admin", __name__)
logger = get_logger(__name__)
_MOJIBAKE_MARKERS = ("\u00e0\u00a4", "\u00e0\u00a5")
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_SUSPICIOUS_SEGMENT_RE = re.compile(r"(?:\u00e0\u00a4|\u00e0\u00a5)[^\u0900-\u097F\s]{0,8}(?:[^\u0900-\u097F\s]|\u00e0\u00a4|\u00e0\u00a5)+")
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
_QUESTION_PURGE_BACKUP_TABLE = "question_cleanup_backups"


def _sanitize_admin_anchor(value: str | None) -> str | None:
    if not value:
        return None
    anchor = str(value).strip().lstrip("#")
    if not anchor:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", anchor):
        return None
    return anchor


def _admin_dashboard_redirect(redirect_values: dict[str, str | int], *, anchor: str | None = None):
    location = url_for("admin.admin_dashboard", **redirect_values)
    if anchor:
        location = f"{location}#{anchor}"
    return redirect(location)


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


def _contains_devanagari(value) -> bool:
    return isinstance(value, str) and bool(_DEVANAGARI_RE.search(value))


def _safe_preview(value, limit: int = 120) -> str:
    if value is None:
        return "<none>"
    preview = str(value).replace("\n", "\\n")
    if len(preview) > limit:
        preview = preview[: limit - 3] + "..."
    return preview.encode("unicode_escape", errors="backslashreplace").decode("ascii")


def _decode_with(value: str, source_encoding: str) -> str | None:
    try:
        return value.encode(source_encoding).decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None


def _iter_repair_candidates(value: str):
    seen = {value}
    for source_encoding in ("latin1", "cp1252"):
        candidate = _decode_with(value, source_encoding)
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield f"{source_encoding}->utf8", candidate

            if _contains_mojibake(candidate):
                second_pass = _decode_with(candidate, source_encoding)
                if second_pass and second_pass not in seen:
                    seen.add(second_pass)
                    yield f"{source_encoding}->utf8 twice", second_pass

    latin1_then_cp1252 = _decode_with(value, "latin1")
    if latin1_then_cp1252 and _contains_mojibake(latin1_then_cp1252):
        candidate = _decode_with(latin1_then_cp1252, "cp1252")
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield "latin1->utf8 then cp1252->utf8", candidate

    cp1252_then_latin1 = _decode_with(value, "cp1252")
    if cp1252_then_latin1 and _contains_mojibake(cp1252_then_latin1):
        candidate = _decode_with(cp1252_then_latin1, "latin1")
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield "cp1252->utf8 then latin1->utf8", candidate


def _score_candidate(original: str, candidate: str) -> tuple[int, int, int, int]:
    devanagari_count = sum(1 for char in candidate if _DEVANAGARI_RE.match(char))
    mojibake_count = sum(candidate.count(marker) for marker in _MOJIBAKE_MARKERS)
    original_devanagari_count = sum(1 for char in original if _DEVANAGARI_RE.match(char))
    return (
        devanagari_count,
        -mojibake_count,
        len(candidate) - abs(len(candidate) - len(original)),
        devanagari_count - original_devanagari_count,
    )


def _best_repair_candidate(value: str) -> tuple[str | None, str | None]:
    best_text = None
    best_method = None
    best_score = None

    for method, candidate in _iter_repair_candidates(value):
        if candidate == value:
            continue
        if not _contains_devanagari(candidate):
            continue
        if _contains_mojibake(candidate):
            continue

        score = _score_candidate(value, candidate)
        if best_score is None or score > best_score:
            best_text = candidate
            best_method = method
            best_score = score

    return best_text, best_method


def _repair_mojibake_segments(value: str) -> tuple[str | None, list[dict[str, str]]]:
    repaired_segments: list[dict[str, str]] = []

    def replace_match(match: re.Match[str]) -> str:
        segment = match.group(0)
        repaired, method = _best_repair_candidate(segment)
        if not repaired or repaired == segment:
            return segment
        repaired_segments.append(
            {
                "method": method or "unknown",
                "before": _safe_preview(segment, limit=80),
                "after": _safe_preview(repaired, limit=80),
            }
        )
        return repaired

    updated = _SUSPICIOUS_SEGMENT_RE.sub(replace_match, value)
    if updated == value:
        return None, []
    if _contains_mojibake(updated):
        return None, repaired_segments
    if not _contains_devanagari(updated):
        return None, repaired_segments
    return updated, repaired_segments


def _repair_mojibake_text(value: str) -> tuple[str | None, str | None, list[dict[str, str]]]:
    repaired, method = _best_repair_candidate(value)
    if repaired and repaired != value:
        return repaired, method, []

    segment_repaired, segment_previews = _repair_mojibake_segments(value)
    if segment_repaired and segment_repaired != value:
        return segment_repaired, "segment_repair", segment_previews

    return None, None, segment_previews


def _ensure_question_cleanup_backup_table(conn) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_QUESTION_PURGE_BACKUP_TABLE} (
            backup_id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            question_id BIGINT NOT NULL,
            exam_id BIGINT,
            set_id BIGINT,
            question_text TEXT,
            option_a TEXT,
            option_b TEXT,
            option_c TEXT,
            option_d TEXT,
            correct_option TEXT,
            explanation TEXT,
            image_path TEXT,
            time_limit INTEGER,
            question_created_at TEXT,
            archived_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_QUESTION_PURGE_BACKUP_TABLE}_batch_id ON {_QUESTION_PURGE_BACKUP_TABLE}(batch_id)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_QUESTION_PURGE_BACKUP_TABLE}_question_id ON {_QUESTION_PURGE_BACKUP_TABLE}(question_id)"
    )


def _fetch_all_question_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            question_id,
            exam_id,
            set_id,
            question_text,
            option_a,
            option_b,
            option_c,
            option_d,
            correct_option,
            explanation,
            image_path,
            time_limit,
            created_at
        FROM questions
        ORDER BY question_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _backup_all_questions(conn, *, batch_id: str, rows: list[dict]) -> int:
    archived_at = _utc_now_iso()
    backup_count = 0
    for row in rows:
        conn.execute(
            f"""
            INSERT INTO {_QUESTION_PURGE_BACKUP_TABLE} (
                backup_id, batch_id, question_id, exam_id, set_id, question_text, option_a, option_b,
                option_c, option_d, correct_option, explanation, image_path, time_limit, question_created_at, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                batch_id,
                row["question_id"],
                row.get("exam_id"),
                row.get("set_id"),
                row.get("question_text"),
                row.get("option_a"),
                row.get("option_b"),
                row.get("option_c"),
                row.get("option_d"),
                row.get("correct_option"),
                row.get("explanation"),
                row.get("image_path"),
                row.get("time_limit"),
                row.get("created_at"),
                archived_at,
            ),
        )
        backup_count += 1
    return backup_count


def _delete_all_questions(conn) -> int:
    cursor = conn.execute("DELETE FROM questions")
    return max(int(cursor.rowcount or 0), 0)


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


def _build_question_changes(row: dict) -> tuple[dict[str, str], list[dict[str, str]]]:
    changes: dict[str, str] = {}
    previews: list[dict[str, str]] = []

    for field_name in _MOJIBAKE_FIELDS:
        value = row.get(field_name)
        if not _contains_mojibake(value):
            continue

        repaired, method, segment_previews = _repair_mojibake_text(value)
        if not repaired or repaired == value:
            logger.info(
                "Mojibake repair skipped | question_id=%s field=%s reason=no_valid_devanagari_candidate before=%s",
                row.get("question_id"),
                field_name,
                _safe_preview(value),
            )
            continue

        changes[field_name] = repaired
        previews.append(
            {
                "field": field_name,
                "method": method or "unknown",
                "before": _safe_preview(value),
                "after": _safe_preview(repaired),
            }
        )
        previews.extend(
            {
                "field": field_name,
                "method": f"{method or 'unknown'}:{segment_preview['method']}",
                "before": segment_preview["before"],
                "after": segment_preview["after"],
            }
            for segment_preview in segment_previews
        )

    return changes, previews


def _apply_mojibake_repairs(conn, rows: list[dict]) -> tuple[int, int]:
    updated_rows = 0
    updated_fields = 0
    for row in rows:
        changes, previews = _build_question_changes(row)
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

        for preview in previews:
            logger.info(
                "Mojibake repair field updated | question_id=%s field=%s method=%s before=%s after=%s",
                row.get("question_id"),
                preview["field"],
                preview["method"],
                preview["before"],
                preview["after"],
            )

    return updated_rows, updated_fields


@admin_blueprint.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    current_user = web_identity_service.get_authenticated_user_snapshot()
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


@admin_blueprint.route("/admin/cleanup-questions", methods=["GET"])
def cleanup_questions():
    token = request.args.get("token", "")
    if not token or not hmac.compare_digest(token, SECRET_KEY):
        logger.warning(
            "Question cleanup denied | remote_addr=%s reason=invalid_token",
            request.headers.get("X-Forwarded-For", request.remote_addr),
        )
        return jsonify({"ok": False, "error": "forbidden"}), 403

    batch_id = f"question-cleanup-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    logger.warning("Question cleanup started | batch_id=%s action=backup_then_delete_questions_only", batch_id)

    try:
        with database.connection() as conn:
            _ensure_question_cleanup_backup_table(conn)
            question_rows = _fetch_all_question_rows(conn)
            backup_rows = _backup_all_questions(conn, batch_id=batch_id, rows=question_rows)
            deleted_rows = _delete_all_questions(conn)

        exam_service.invalidate_cache()
        logger.warning(
            "Question cleanup completed | batch_id=%s backup_rows=%s deleted_rows=%s backup_table=%s",
            batch_id,
            backup_rows,
            deleted_rows,
            _QUESTION_PURGE_BACKUP_TABLE,
        )
        return jsonify(
            {
                "ok": True,
                "batch_id": batch_id,
                "backup_rows": backup_rows,
                "deleted_rows": deleted_rows,
                "backup_table": _QUESTION_PURGE_BACKUP_TABLE,
                "note": "Only the questions table was cleaned. Users, payments, premium, and login data were not changed.",
            }
        )
    except Exception as exc:
        logger.exception("Question cleanup failed | batch_id=%s", batch_id)
        return jsonify({"ok": False, "batch_id": batch_id, "error": str(exc)}), 500


@admin_blueprint.route("/admin", methods=["GET", "POST"])
@admin_required
def admin_dashboard():
    current_user = web_identity_service.get_authenticated_user_snapshot()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        current_query = (request.args.get("q") or request.form.get("q") or "").strip()
        redirect_values: dict[str, str | int] = {}
        return_anchor = _sanitize_admin_anchor(request.form.get("return_anchor") or request.args.get("return_anchor"))
        if current_query:
            redirect_values["q"] = current_query

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
            elif action == "save_question":
                question_id = int(request.form.get("question_id", "0") or "0") or None
                question_result = (
                    web_admin_service.update_question(
                        question_id=question_id,
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
                    if question_id
                    else web_admin_service.add_question(
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
                )
                operation = question_result.get("operation") or "saved"
                if operation == "created":
                    flash("Question saved successfully.", "success")
                elif operation == "updated":
                    flash("Question updated successfully.", "success")
                else:
                    flash("Duplicate question in this set was replaced with the latest version.", "success")
            elif action == "bulk_import":
                created = web_admin_service.bulk_import_questions(
                    set_id=int(request.form.get("set_id", "0")),
                    raw_text=request.form.get("bulk_payload", ""),
                )
                replaced_count = sum(1 for item in created if item.get("operation") == "replaced")
                created_count = sum(1 for item in created if item.get("operation") == "created")
                updated_count = sum(1 for item in created if item.get("operation") == "updated")
                flash(
                    f"Bulk import completed: {created_count} created, {replaced_count} replaced, {updated_count} updated.",
                    "success",
                )
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
            posted_question_id = int(request.form.get("question_id", "0") or "0")
            if posted_question_id:
                redirect_values["edit_question_id"] = posted_question_id
            flash(str(exc), "error")
        return _admin_dashboard_redirect(redirect_values, anchor=return_anchor)

    search_query = (request.args.get("q") or "").strip()
    edit_question_id_raw = (request.args.get("edit_question_id") or "").strip()
    edit_question_id = None
    if edit_question_id_raw.isdigit():
        edit_question_id = int(edit_question_id_raw)
    dashboard = web_admin_service.dashboard_page_data(
        search_text=search_query,
        edit_question_id=edit_question_id,
    )

    admin_return_anchor = _sanitize_admin_anchor(request.args.get("return_anchor"))

    return render_template(
        "admin_dashboard.html",
        page_title="Admin Panel",
        support_telegram=SUPPORT_TELEGRAM,
        admin_authenticated=True,
        current_user=current_user,
        search_query=search_query,
        admin_return_anchor=admin_return_anchor,
        **dashboard,
    )
