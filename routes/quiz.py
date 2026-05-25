from flask import Blueprint, flash, redirect, render_template, request, send_file, session, url_for

from db.database import database
from utils.logging_utils import get_logger

from routes.auth import login_required
from services.exam_service_db import exam_service
from services.premium_service_db import premium_service
from services.quiz_settings_service import quiz_settings_service
from services.user_service_db import user_service
from services.web_identity_service import web_identity_service
from services.web_quiz_pdf_service import web_quiz_pdf_service
from services.web_quiz_service import web_quiz_service


quiz_blueprint = Blueprint("quiz", __name__)
logger = get_logger(__name__)


def _quiz_session_snapshot() -> dict:
    return dict(session.get(web_quiz_service.SESSION_STORAGE_KEY) or {})


def _store_quiz_session_snapshot(user_id: int) -> dict:
    snapshot = web_quiz_service.build_session_snapshot(user_id) or {}
    session[web_quiz_service.SESSION_STORAGE_KEY] = web_quiz_service.build_session_reference(user_id) or {}
    return snapshot


def _clear_quiz_session_snapshot() -> None:
    session.pop(web_quiz_service.SESSION_STORAGE_KEY, None)


def _restart_quiz_session(user_id: int, *, message: str) -> None:
    web_quiz_service.clear_session(user_id)
    session.pop("active_result", None)
    session.pop("last_quiz_result", None)
    _clear_quiz_session_snapshot()
    flash(message, "error")


def _handle_play_action_result(user_id: int, result: dict | None, *, action: str):
    if not result:
        logger.warning("Quiz play action failed gracefully | user_id=%s action=%s", user_id, action)
        _restart_quiz_session(user_id, message="Your quiz session expired or was invalid. Please start again.")
        return redirect(url_for("quiz.quiz_start"))

    snapshot = _store_quiz_session_snapshot(user_id)
    logger.info("Quiz answer route | user_id=%s index=%s action=%s", user_id, snapshot.get("index"), action)
    return redirect(url_for("quiz.play"))


def _attempt_metrics(item: dict) -> dict:
    attempted = int(item.get("correct_count") or 0) + int(item.get("wrong_count") or 0)
    skipped = int(item.get("skipped_count") or 0)
    requested_count = int(item.get("requested_count") or 0)
    completed = attempted + skipped
    score = float(item.get("correct_count") or 0) - (float(item.get("wrong_count") or 0) * 0.25)
    accuracy = (float(item.get("correct_count") or 0) / attempted * 100) if attempted else 0.0
    progress_percent = (completed / requested_count * 100) if requested_count else 0.0
    return {
        **item,
        "attempted": attempted,
        "completed": completed,
        "score": score,
        "accuracy": accuracy,
        "progress_percent": progress_percent,
    }


def _attempt_rank_key(item: dict) -> tuple:
    return (
        float(item.get("score") or 0),
        float(item.get("accuracy") or 0),
        int(item.get("correct_count") or 0),
        -int(item.get("wrong_count") or 0),
        str(item.get("created_at") or ""),
        int(item.get("attempt_id") or 0),
    )


def _load_user_attempts_for_exam(user_id: int, exam_id: int) -> list[dict]:
    with database.connection() as conn:
        rows = conn.execute(
            """
            SELECT
                qa.*,
                s.title AS set_title,
                e.title AS exam_title
            FROM quiz_attempts qa
            JOIN exam_sets s ON s.set_id = qa.set_id
            JOIN exams e ON e.exam_id = s.exam_id
            WHERE qa.user_id = ? AND s.exam_id = ?
            ORDER BY qa.created_at DESC, qa.attempt_id DESC
            """,
            (user_id, exam_id),
        ).fetchall()

    return [_attempt_metrics(dict(row)) for row in rows]


def _summarize_attempts_by_set(attempts: list[dict]) -> dict[int, dict]:
    summaries: dict[int, dict] = {}
    for attempt in attempts:
        set_id = int(attempt.get("set_id") or 0)
        bucket = summaries.setdefault(set_id, {"latest": attempt, "best": attempt})
        if _attempt_rank_key(attempt) > _attempt_rank_key(bucket["best"]):
            bucket["best"] = attempt
    return summaries


def _load_rank_map_for_exam(exam_id: int) -> dict[int, dict[int, int]]:
    with database.connection() as conn:
        rows = conn.execute(
            """
            SELECT
                qa.*,
                s.exam_id
            FROM quiz_attempts qa
            JOIN exam_sets s ON s.set_id = qa.set_id
            WHERE s.exam_id = ?
            ORDER BY qa.created_at DESC, qa.attempt_id DESC
            """,
            (exam_id,),
        ).fetchall()

    best_attempts: dict[tuple[int, int], dict] = {}
    for row in rows:
        attempt = _attempt_metrics(dict(row))
        key = (int(attempt.get("set_id") or 0), int(attempt.get("user_id") or 0))
        current = best_attempts.get(key)
        if current is None or _attempt_rank_key(attempt) > _attempt_rank_key(current):
            best_attempts[key] = attempt

    grouped: dict[int, list[dict]] = {}
    for attempt in best_attempts.values():
        grouped.setdefault(int(attempt.get("set_id") or 0), []).append(attempt)

    rank_map: dict[int, dict[int, int]] = {}
    for set_id, entries in grouped.items():
        ordered = sorted(entries, key=_attempt_rank_key, reverse=True)
        rank_map[set_id] = {
            int(entry.get("user_id") or 0): index
            for index, entry in enumerate(ordered, start=1)
        }
    return rank_map


def _question_count_options_for_set(exam_item: dict, set_item: dict) -> list[dict]:
    options = []
    question_count = int(set_item.get("question_count") or 0)
    if (
        str(exam_item.get("title") or "").strip().upper() == web_quiz_service.FULL_SET_EXAM_TITLE
        and str(set_item.get("title") or "").strip() == web_quiz_service.FULL_SET_TITLE
        and question_count
    ):
        options.append({"value": question_count, "label": f"All {question_count} Questions"})

    for count in web_quiz_service.QUIZ_COUNT_OPTIONS:
        if not any(int(option["value"]) == int(count) for option in options):
            options.append({"value": int(count), "label": f"{count} Questions"})
    return options


def _active_set_state(active_session: dict | None, set_id: int) -> dict | None:
    if not active_session or int(active_session.get("set_id") or 0) != int(set_id):
        return None

    total_questions = len(active_session.get("questions") or [])
    completed_questions = len(active_session.get("responses") or [])
    progress_percent = (completed_questions / total_questions * 100) if total_questions else 0.0
    return {
        "set_id": int(set_id),
        "started_at": active_session.get("started_at"),
        "completed_questions": completed_questions,
        "total_questions": total_questions,
        "progress_percent": progress_percent,
        "awaiting_next": bool(active_session.get("awaiting_next")),
        "is_paused": bool(active_session.get("is_paused")),
    }


def _progress_text(latest_attempt: dict | None, active_state: dict | None) -> str:
    if active_state:
        return f"{active_state['completed_questions']} of {active_state['total_questions']} questions completed"
    if latest_attempt:
        return f"{latest_attempt['completed']} of {latest_attempt['requested_count']} questions completed"
    return "Not attempted yet"


def _start_selected_set(
    user_id: int,
    *,
    set_id: int,
    requested_count: int,
    failure_endpoint: str,
    failure_values: dict | None = None,
):
    failure_values = failure_values or {}
    started, error = web_quiz_service.start_quiz(user_id, set_id, requested_count)
    if error:
        flash(error, "error")
        return redirect(url_for(failure_endpoint, **failure_values))

    session.pop("active_result", None)
    session.pop("last_quiz_result", None)
    snapshot = _store_quiz_session_snapshot(user_id)
    logger.info("Quiz start route | user_id=%s set_id=%s total=%s", user_id, set_id, snapshot.get("total"))
    flash(
        f"Quiz started for {started.get('set_title') or 'Quiz Set'} with {len(started['questions'])} questions.",
        "success",
    )
    return redirect(url_for("quiz.play"))


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

        return _start_selected_set(
            user_id,
            set_id=set_id,
            requested_count=requested_count,
            failure_endpoint="quiz.quiz_start",
        )

    exams = [dict(item) for item in exam_service.get_exams()]
    exam_hierarchy = exam_service.list_exam_hierarchy()
    exam_catalog = web_quiz_service.list_exam_catalog(user_id)
    return render_template(
        "quiz_start.html",
        page_title="Start Quiz",
        user=user,
        exams=exams,
        exam_hierarchy=exam_hierarchy,
        exam_catalog=exam_catalog,
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@quiz_blueprint.route("/quiz/<int:exam_id>/sets", methods=["GET", "POST"])
@login_required
def exam_sets(exam_id: int):
    user = web_identity_service.get_authenticated_user_snapshot()
    user_id = web_identity_service.get_authenticated_user_id()
    if not user_id:
        flash("Please log in to continue.", "error")
        return redirect(url_for("auth.login"))

    exam = exam_service.get_exam(exam_id)
    if not exam:
        flash("The selected exam could not be found.", "error")
        return redirect(url_for("quiz.quiz_start"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        try:
            set_id = int(request.form.get("set_id", "0"))
        except ValueError:
            flash("Please choose a valid set.", "error")
            return redirect(url_for("quiz.exam_sets", exam_id=exam_id))

        if action == "continue":
            active_session = web_quiz_service.get_session(user_id)
            if active_session and int(active_session.get("set_id") or 0) == set_id:
                if active_session.get("is_paused"):
                    resume_result = web_quiz_service.resume_quiz(user_id, set_id=set_id)
                    if resume_result and resume_result.get("warning"):
                        flash(str(resume_result["warning"]), "error")
                return redirect(url_for("quiz.play"))
            flash("No active quiz session was found for this set.", "error")
            return redirect(url_for("quiz.exam_sets", exam_id=exam_id))

        try:
            requested_count = int(request.form.get("question_count", "20"))
        except ValueError:
            flash("Please select a valid question count.", "error")
            return redirect(url_for("quiz.exam_sets", exam_id=exam_id))

        set_item = exam_service.get_set(set_id)
        if not set_item or int(set_item.get("exam_id") or 0) != int(exam_id):
            flash("Please choose a valid set from this exam.", "error")
            return redirect(url_for("quiz.exam_sets", exam_id=exam_id))

        return _start_selected_set(
            user_id,
            set_id=set_id,
            requested_count=requested_count,
            failure_endpoint="quiz.exam_sets",
            failure_values={"exam_id": exam_id},
        )

    premium_active = premium_service.is_premium(user_id)
    admin_access = user_service.is_admin(user_id)
    active_session = web_quiz_service.get_session(user_id)
    quiz_settings = quiz_settings_service.get_settings()
    attempt_summaries = _summarize_attempts_by_set(_load_user_attempts_for_exam(user_id, exam_id))
    rank_map = _load_rank_map_for_exam(exam_id)

    sets = []
    for set_item in exam_service.get_sets(exam_id):
        set_id = int(set_item.get("set_id") or 0)
        locked = bool(int(set_item.get("is_premium_locked", 0)))
        has_access = (not locked) or premium_active or admin_access
        latest_attempt = attempt_summaries.get(set_id, {}).get("latest")
        best_attempt = attempt_summaries.get(set_id, {}).get("best")
        active_state = _active_set_state(active_session, set_id)
        progress_percent = (
            active_state["progress_percent"]
            if active_state
            else float(latest_attempt.get("progress_percent") or 0)
            if latest_attempt
            else 0.0
        )

        sets.append(
            {
                **dict(set_item),
                "locked": locked,
                "has_access": has_access,
                "latest_attempt": latest_attempt,
                "best_attempt": best_attempt,
                "active_state": active_state,
                "progress_text": _progress_text(latest_attempt, active_state),
                "progress_percent": progress_percent,
                "accuracy": float(best_attempt.get("accuracy") or 0) if best_attempt else 0.0,
                "last_attempted_at": (
                    latest_attempt.get("created_at")
                    if latest_attempt
                    else active_state.get("started_at")
                    if active_state
                    else None
                ),
                "rank": rank_map.get(set_id, {}).get(int(user_id)),
                "question_count_options": _question_count_options_for_set(exam, set_item),
                "action_label": "Continue" if active_state else "Retry" if latest_attempt else "Play",
                "attempt_count": web_quiz_service.count_attempts_for_set(user_id, set_id),
            }
        )

    return render_template(
        "quiz_exam_sets.html",
        page_title=f"{exam.get('title') or 'Exam'} Sets",
        user=user,
        exam=exam,
        sets=sets,
        quiz_settings=quiz_settings,
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
    if quiz_session.get("is_paused") and request.method == "GET":
        set_item = exam_service.get_set(int(quiz_session.get("set_id") or 0)) or {}
        flash("This quiz is paused. Use Resume from the set page to continue.", "success")
        return redirect(url_for("quiz.exam_sets", exam_id=int(set_item.get("exam_id") or 0)))

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "pause":
            pause_result = web_quiz_service.pause_quiz(user_id)
            if not pause_result:
                flash("No active quiz session was found.", "error")
                return redirect(url_for("quiz.quiz_start"))
            if pause_result.get("message"):
                flash(str(pause_result["message"]), "error" if not pause_result.get("ok") else "success")
            if pause_result.get("warning"):
                flash(str(pause_result["warning"]), "error")
            set_item = exam_service.get_set(int(quiz_session.get("set_id") or 0)) or {}
            return redirect(url_for("quiz.exam_sets", exam_id=int(set_item.get("exam_id") or 0)))

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
            try:
                result = web_quiz_service.answer_question(user_id, selected_index, action="answer")
            except Exception:
                logger.exception("Quiz answer route crashed | user_id=%s action=answer", user_id)
                _restart_quiz_session(user_id, message="We restarted your quiz because the previous session became unavailable.")
                return redirect(url_for("quiz.quiz_start"))
            return _handle_play_action_result(user_id, result, action="answer")

        if action == "skip":
            try:
                result = web_quiz_service.answer_question(user_id, None, action="skip")
            except Exception:
                logger.exception("Quiz answer route crashed | user_id=%s action=skip", user_id)
                _restart_quiz_session(user_id, message="We restarted your quiz because the previous session became unavailable.")
                return redirect(url_for("quiz.quiz_start"))
            return _handle_play_action_result(user_id, result, action="skip")

        if action == "timeout":
            try:
                result = web_quiz_service.answer_question(user_id, None, action="timeout")
            except Exception:
                logger.exception("Quiz answer route crashed | user_id=%s action=timeout", user_id)
                _restart_quiz_session(user_id, message="We restarted your quiz because the previous session became unavailable.")
                return redirect(url_for("quiz.quiz_start"))
            return _handle_play_action_result(user_id, result, action="timeout")

        if action == "next":
            if web_quiz_service.next_question(user_id):
                snapshot = _store_quiz_session_snapshot(user_id)
                logger.info("Quiz next route | user_id=%s next_index=%s", user_id, snapshot.get("index"))
                return redirect(url_for("quiz.play"))
            web_quiz_service.submit_quiz(user_id, ended_reason="completed")
            session.pop("last_quiz_result", None)
            _clear_quiz_session_snapshot()
            return redirect(url_for("quiz.result"))

        if action == "submit":
            web_quiz_service.submit_quiz(user_id, ended_reason="submitted")
            session.pop("last_quiz_result", None)
            session.pop("active_result", None)
            _clear_quiz_session_snapshot()
            return redirect(url_for("quiz.result"))

        return redirect(url_for("quiz.play"))

    quiz_snapshot = web_quiz_service.build_session_snapshot(user_id) or {}
    if quiz_snapshot:
        session[web_quiz_service.SESSION_STORAGE_KEY] = web_quiz_service.build_session_reference(user_id) or {}
    active_result = quiz_snapshot.get("last_result") if quiz_snapshot.get("awaiting_next") else None

    logger.info("Quiz play render | user_id=%s index=%s total=%s has_result=%s", user_id, quiz_snapshot.get("index"), quiz_snapshot.get("total"), bool(active_result))
    question = quiz_snapshot.get("current_question")
    if not question and active_result:
        question = {
            "number": int(active_result.get("answered_index") or 0) + 1,
            "total": int(quiz_snapshot.get("total") or 0),
            "remaining_seconds": 0,
            "question_text": active_result.get("question_text") or "",
            "image_path": active_result.get("image_path"),
        }
    if not question:
        web_quiz_service.submit_quiz(user_id, ended_reason="completed")
        session.pop("last_quiz_result", None)
        session.pop("active_result", None)
        _clear_quiz_session_snapshot()
        return redirect(url_for("quiz.result"))

    current_set = {"title": quiz_snapshot.get("set_title") or "Quiz Set"}
    return render_template(
        "play.html",
        page_title="Play Quiz",
        body_class="quiz-play-page",
        compact_quiz_nav=True,
        user=user,
        current_set=current_set,
        quiz_session=quiz_snapshot,
        question=question,
        active_result=active_result,
        rules_text=[
            "Each correct answer gives +1 mark.",
            "Each wrong answer deducts 0.25 mark.",
            "Skipping or timeout gives 0 mark.",
            "Use Pause only if needed and resume from the same set.",
            "Do not refresh during a question unless necessary.",
        ],
        media_route_name="media_file",
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@quiz_blueprint.route("/result", methods=["GET", "POST"])
@login_required
def result():
    user = web_identity_service.get_authenticated_user_snapshot()
    user_id = web_identity_service.get_authenticated_user_id()
    if not user_id:
        flash("Please log in to continue.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        if action == "report_question":
            try:
                web_quiz_service.submit_question_report(
                    user_id=user_id,
                    question_id=int(request.form.get("question_id", "0")),
                    set_id=int(request.form.get("set_id", "0")),
                    reason=request.form.get("reason", ""),
                )
                flash("Your report has been submitted to the admin team.", "success")
            except Exception as exc:
                flash(str(exc), "error")
        return redirect(url_for("quiz.result"))

    summary = web_quiz_service.load_completed_summary(user_id) or session.get("last_quiz_result")
    if not summary:
        flash("No quiz result found. Start a quiz first.", "error")
        return redirect(url_for("quiz.quiz_start"))

    summary = web_quiz_service.build_summary_display_data(summary, user)
    leaderboard = web_quiz_service.leaderboard_for_set(int(summary.get("set_id") or 0), current_user_id=user_id)
    summary["rank"] = leaderboard.get("current_rank")
    current_set = {"title": summary.get("set_title") or "Quiz"}
    performance = web_quiz_service.user_performance_snapshot(user_id, limit=5)
    attempted_review_items = [item for item in summary.get("review_items", []) if item.get("action") == "answer"]
    return render_template(
        "result.html",
        page_title="Quiz Result",
        body_class="quiz-result-page",
        user=user,
        summary=summary,
        current_set=current_set,
        performance=performance,
        attempted_review_items=attempted_review_items,
        leaderboard_rows=leaderboard.get("top_ten", []),
        rank_zone_rows=leaderboard.get("rank_zone", []),
        show_rank_zone=bool(user.get("is_premium")),
        admin_authenticated=web_identity_service.is_admin_authenticated(),
    )


@quiz_blueprint.route("/result/pdf", methods=["GET"])
@login_required
def result_pdf():
    user = web_identity_service.get_authenticated_user_snapshot()
    user_id = web_identity_service.get_authenticated_user_id()
    if not user_id:
        flash("Please log in to continue.", "error")
        return redirect(url_for("auth.login"))

    summary = web_quiz_service.load_completed_summary(user_id) or session.get("last_quiz_result")
    if not summary:
        flash("No quiz result found. Start a quiz first.", "error")
        return redirect(url_for("quiz.quiz_start"))

    summary = web_quiz_service.build_summary_display_data(summary, user)
    leaderboard = web_quiz_service.leaderboard_for_set(int(summary.get("set_id") or 0), current_user_id=user_id)
    summary["rank"] = leaderboard.get("current_rank")

    file_path = web_quiz_pdf_service.generate_result_pdf(
        user_name=user.get("full_name") or "User",
        summary=summary,
        leaderboard_rows=leaderboard.get("top_ten", []),
        rank_zone_rows=leaderboard.get("rank_zone", []),
        show_rank_zone=bool(user.get("is_premium")),
    )
    return send_file(file_path, as_attachment=True, download_name=file_path.name)
