from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, url_for

from config import ADMIN_PASSWORD, SUPPORT_TELEGRAM
from services.payment_service_db import payment_service
from services.web_admin_service import web_admin_service
from services.web_identity_service import web_identity_service


admin_blueprint = Blueprint("admin", __name__)


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
