from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from routes.auth import login_required
from services.exam_service_db import exam_service
from services.web_identity_service import web_identity_service
from services.web_quiz_service import web_quiz_service


quiz_blueprint = Blueprint("quiz", __name__)


@quiz_blueprint.route("/quiz", methods=["GET", "POST"])
@login_required
def quiz_start():
    user = web_identity_service.get_authenticated_user()
    if request.method == "POST":
        try:
            set_id = int(request.form.get("set_id", "0"))
            requested_count = int(request.form.get("question_count", "20"))
        except ValueError:
            flash("Please select a valid quiz set and question count.", "error")
            return redirect(url_for("quiz.quiz_start"))

        started, error = web_quiz_service.start_quiz(user["user_id"], set_id, requested_count)
        if error:
            flash(error, "error")
            return redirect(url_for("quiz.quiz_start"))

        current_set = exam_service.get_set(set_id)
        session["active_result"] = None
        session["last_quiz_result"] = None
        flash(
            f"Quiz started for {current_set['title']} with {len(started['questions'])} questions.",
            "success",
        )
        return redirect(url_for("quiz.play"))

    catalog = web_quiz_service.list_exam_catalog(user["user_id"])
    return render_template(
        "quiz_start.html",
        page_title="Start Quiz",
        user=user,
        catalog=catalog,
        question_counts=web_quiz_service.QUIZ_COUNT_OPTIONS,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@quiz_blueprint.route("/play", methods=["GET", "POST"])
@login_required
def play():
    user = web_identity_service.get_authenticated_user()
    quiz_session = web_quiz_service.get_session(user["user_id"])
    if not quiz_session:
        flash("Start a quiz first.", "error")
        return redirect(url_for("quiz.quiz_start"))

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "answer":
            selected_raw = request.form.get("selected_option")
            if selected_raw is None:
                flash("Select an option or use Skip.", "error")
                return redirect(url_for("quiz.play"))
            try:
                selected_index = int(selected_raw)
            except ValueError:
                flash("Please select a valid option.", "error")
                return redirect(url_for("quiz.play"))
            result = web_quiz_service.answer_question(user["user_id"], selected_index, action="answer")
            session["active_result"] = result or session.get("active_result")
            return redirect(url_for("quiz.play"))

        if action == "skip":
            result = web_quiz_service.answer_question(user["user_id"], None, action="skip")
            session["active_result"] = result or session.get("active_result")
            return redirect(url_for("quiz.play"))

        if action == "timeout":
            result = web_quiz_service.answer_question(user["user_id"], None, action="timeout")
            session["active_result"] = result or session.get("active_result")
            return redirect(url_for("quiz.play"))

        if action == "next":
            session["active_result"] = None
            if web_quiz_service.next_question(user["user_id"]):
                return redirect(url_for("quiz.play"))
            summary = web_quiz_service.submit_quiz(user["user_id"], ended_reason="completed")
            session["last_quiz_result"] = summary
            return redirect(url_for("quiz.result"))

        if action == "submit":
            summary = web_quiz_service.submit_quiz(user["user_id"], ended_reason="submitted")
            session["last_quiz_result"] = summary
            session["active_result"] = None
            return redirect(url_for("quiz.result"))

        return redirect(url_for("quiz.play"))

    question = web_quiz_service.get_current_question(user["user_id"])
    if not question:
        summary = web_quiz_service.submit_quiz(user["user_id"], ended_reason="completed")
        session["last_quiz_result"] = summary
        session["active_result"] = None
        return redirect(url_for("quiz.result"))

    active_result = session.get("active_result")
    current_set = exam_service.get_set(quiz_session["set_id"])
    return render_template(
        "play.html",
        page_title="Play Quiz",
        user=user,
        current_set=current_set,
        quiz_session=quiz_session,
        question=question,
        active_result=active_result,
        media_route_name="media_file",
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@quiz_blueprint.route("/result")
@login_required
def result():
    user = web_identity_service.get_authenticated_user()
    summary = session.get("last_quiz_result")
    if not summary:
        flash("No quiz result found. Start a quiz first.", "error")
        return redirect(url_for("quiz.quiz_start"))

    set_item = exam_service.get_set(summary["set_id"]) if summary.get("set_id") else None
    performance = web_quiz_service.user_performance_snapshot(user["user_id"], limit=5)
    attempted_review_items = [item for item in summary.get("review_items", []) if item.get("action") == "answer"]
    return render_template(
        "result.html",
        page_title="Quiz Result",
        user=user,
        summary=summary,
        current_set=set_item,
        performance=performance,
        attempted_review_items=attempted_review_items,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )
