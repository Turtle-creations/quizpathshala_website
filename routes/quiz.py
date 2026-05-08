from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from utils.logging_utils import get_logger

from routes.auth import login_required
from services.web_identity_service import web_identity_service
from services.web_quiz_service import web_quiz_service


quiz_blueprint = Blueprint("quiz", __name__)
logger = get_logger(__name__)


def _quiz_session_snapshot() -> dict:
    return dict(session.get(web_quiz_service.SESSION_STORAGE_KEY) or {})


def _store_quiz_session_snapshot(user_id: int) -> dict:
    snapshot = web_quiz_service.build_session_snapshot(user_id) or {}
    session[web_quiz_service.SESSION_STORAGE_KEY] = snapshot
    return snapshot


def _clear_quiz_session_snapshot() -> None:
    session.pop(web_quiz_service.SESSION_STORAGE_KEY, None)


@quiz_blueprint.route("/quiz", methods=["GET", "POST"])
@login_required
def quiz_start():
    user = web_identity_service.get_authenticated_user_snapshot()
    user_id = web_identity_service.get_authenticated_user_id()
    if not user_id:
        flash("Please log in to continue.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        try:
            set_id = int(request.form.get("set_id", "0"))
            requested_count = int(request.form.get("question_count", "20"))
        except ValueError:
            flash("Please select a valid quiz set and question count.", "error")
            return redirect(url_for("quiz.quiz_start"))

        started, error = web_quiz_service.start_quiz(user_id, set_id, requested_count)
        if error:
            flash(error, "error")
            return redirect(url_for("quiz.quiz_start"))

        session["active_result"] = None
        session["last_quiz_result"] = None
        snapshot = _store_quiz_session_snapshot(user_id)
        logger.info("Quiz start route | user_id=%s set_id=%s total=%s", user_id, set_id, snapshot.get("total"))
        flash(
            f"Quiz started for {started.get('set_title') or 'Quiz Set'} with {len(started['questions'])} questions.",
            "success",
        )
        return redirect(url_for("quiz.play"))

    catalog = web_quiz_service.list_exam_catalog(user_id)
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
    user = web_identity_service.get_authenticated_user_snapshot()
    user_id = web_identity_service.get_authenticated_user_id()
    if not user_id:
        flash("Please log in to continue.", "error")
        return redirect(url_for("auth.login"))

    quiz_session = web_quiz_service.get_session(user_id)
    if not quiz_session:
        restored_snapshot = _quiz_session_snapshot()
        logger.warning(
            "Quiz play missing active session | user_id=%s has_snapshot=%s snapshot_index=%s",
            user_id,
            bool(restored_snapshot),
            restored_snapshot.get("index") if restored_snapshot else None,
        )
        _clear_quiz_session_snapshot()
        flash("Your previous quiz session was unavailable. Please start again.", "error")
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
            result = web_quiz_service.answer_question(user_id, selected_index, action="answer")
            session["active_result"] = result or session.get("active_result")
            snapshot = _store_quiz_session_snapshot(user_id)
            logger.info("Quiz answer route | user_id=%s index=%s action=answer", user_id, snapshot.get("index"))
            return redirect(url_for("quiz.play"))

        if action == "skip":
            result = web_quiz_service.answer_question(user_id, None, action="skip")
            session["active_result"] = result or session.get("active_result")
            snapshot = _store_quiz_session_snapshot(user_id)
            logger.info("Quiz answer route | user_id=%s index=%s action=skip", user_id, snapshot.get("index"))
            return redirect(url_for("quiz.play"))

        if action == "timeout":
            result = web_quiz_service.answer_question(user_id, None, action="timeout")
            session["active_result"] = result or session.get("active_result")
            snapshot = _store_quiz_session_snapshot(user_id)
            logger.info("Quiz answer route | user_id=%s index=%s action=timeout", user_id, snapshot.get("index"))
            return redirect(url_for("quiz.play"))

        if action == "next":
            session["active_result"] = None
            if web_quiz_service.next_question(user_id):
                snapshot = _store_quiz_session_snapshot(user_id)
                logger.info("Quiz next route | user_id=%s next_index=%s", user_id, snapshot.get("index"))
                return redirect(url_for("quiz.play"))
            summary = web_quiz_service.submit_quiz(user_id, ended_reason="completed")
            session["last_quiz_result"] = summary
            _clear_quiz_session_snapshot()
            return redirect(url_for("quiz.result"))

        if action == "submit":
            summary = web_quiz_service.submit_quiz(user_id, ended_reason="submitted")
            session["last_quiz_result"] = summary
            session["active_result"] = None
            _clear_quiz_session_snapshot()
            return redirect(url_for("quiz.result"))

        return redirect(url_for("quiz.play"))

    quiz_snapshot = _quiz_session_snapshot() or _store_quiz_session_snapshot(user_id)
    logger.info("Quiz play render | user_id=%s index=%s total=%s has_result=%s", user_id, quiz_snapshot.get("index"), quiz_snapshot.get("total"), bool(session.get("active_result")))
    question = quiz_snapshot.get("current_question")
    if not question:
        summary = web_quiz_service.submit_quiz(user_id, ended_reason="completed")
        session["last_quiz_result"] = summary
        session["active_result"] = None
        _clear_quiz_session_snapshot()
        return redirect(url_for("quiz.result"))

    active_result = session.get("active_result")
    current_set = {"title": quiz_snapshot.get("set_title") or "Quiz Set"}
    return render_template(
        "play.html",
        page_title="Play Quiz",
        user=user,
        current_set=current_set,
        quiz_session=quiz_snapshot,
        question=question,
        active_result=active_result,
        media_route_name="media_file",
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@quiz_blueprint.route("/result")
@login_required
def result():
    user = web_identity_service.get_authenticated_user_snapshot()
    user_id = web_identity_service.get_authenticated_user_id()
    if not user_id:
        flash("Please log in to continue.", "error")
        return redirect(url_for("auth.login"))

    summary = session.get("last_quiz_result")
    if not summary:
        flash("No quiz result found. Start a quiz first.", "error")
        return redirect(url_for("quiz.quiz_start"))

    current_set = {"title": summary.get("set_title") or "Quiz"}
    performance = web_quiz_service.user_performance_snapshot(user_id, limit=5)
    attempted_review_items = [item for item in summary.get("review_items", []) if item.get("action") == "answer"]
    return render_template(
        "result.html",
        page_title="Quiz Result",
        user=user,
        summary=summary,
        current_set=current_set,
        performance=performance,
        attempted_review_items=attempted_review_items,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )
