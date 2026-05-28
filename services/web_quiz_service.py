from copy import deepcopy
from datetime import datetime, timezone
import json
import random
import secrets
import time

from config import DEFAULT_QUESTION_TIME
from db.database import database
from services.exam_service_db import exam_service
from services.premium_service_db import premium_service
from services.quiz_settings_service import quiz_settings_service
from services.user_service_db import user_service
from utils.timezone_utils import format_user_datetime
from utils.logging_utils import get_logger


class WebQuizService:
    QUIZ_COUNT_OPTIONS = (20, 50, 100)
    MAX_QUESTIONS_PER_QUIZ = 100
    BONUS_INCREMENT_SECONDS = 5
    MAX_BONUS_SECONDS = 60
    SESSION_STORAGE_KEY = "active_quiz_session"
    ACTIVE_SESSION_TABLE = "web_quiz_sessions"
    FULL_SET_EXAM_TITLE = "SSE"
    FULL_SET_TITLE = "Set-1"
    BREAK_WARNING_MESSAGE = "You have taken too many breaks. Please complete the quiz or come back later."

    def __init__(self) -> None:
        self.sessions: dict[int, dict] = {}
        self.logger = get_logger(__name__)

    def list_exam_catalog(self, user_id: int) -> list[dict]:
        catalog = []
        premium_active = premium_service.is_premium(user_id)
        admin_access = user_service.is_admin(user_id)
        for exam in exam_service.get_exams():
            sets = []
            for set_item in exam_service.get_sets(exam["exam_id"]):
                locked = bool(int(set_item.get("is_premium_locked", 0)))
                has_access = (not locked) or premium_active or admin_access
                sets.append(
                    {
                        **set_item,
                        "locked": locked,
                        "has_access": has_access,
                    }
                )
            catalog.append({**exam, "sets": sets})
        return catalog

    def can_access_set(self, user_id: int, set_id: int) -> bool:
        set_item = exam_service.get_set(set_id)
        if not set_item:
            return False
        if not int(set_item.get("is_premium_locked", 0)):
            return True
        return premium_service.is_premium(user_id) or user_service.is_admin(user_id)

    def start_quiz(self, user_id: int, set_id: int, requested_count: int) -> tuple[dict | None, str | None]:
        set_item = exam_service.get_set(set_id)
        if not set_item:
            return None, "This quiz set is not available."

        exam_item = exam_service.get_exam(int(set_item.get("exam_id") or 0))
        settings = quiz_settings_service.get_settings()
        existing_session = self.get_session(user_id)
        if (
            existing_session
            and settings.get("allow_resume", True)
            and int(existing_session.get("set_id") or 0) == int(set_id)
        ):
            return None, "You already have an unfinished quiz in this set. Please resume it first."

        self.clear_session(user_id)

        locked = bool(int(set_item.get("is_premium_locked", 0)))
        if locked and not (premium_service.is_premium(user_id) or user_service.is_admin(user_id)):
            return None, "This quiz set is premium-only."

        max_attempts = int(settings.get("max_attempts_per_set") or 0)
        if max_attempts > 0 and not user_service.is_admin(user_id):
            attempt_count = self.count_attempts_for_set(user_id, set_id)
            if attempt_count >= max_attempts:
                return None, f"You have reached the maximum of {max_attempts} attempts for this set."

        use_full_set = self._should_force_full_set(exam_item, set_item)
        if not use_full_set and int(requested_count) not in self.QUIZ_COUNT_OPTIONS:
            return None, "Please choose 20, 50, or 100 questions."

        question_pool = [self._prepare_question(item) for item in exam_service.get_questions(set_id)]
        if not question_pool:
            return None, "No questions are available in this set yet."

        random.shuffle(question_pool)
        if use_full_set:
            actual_count = len(question_pool)
            questions = question_pool
        else:
            actual_count = min(max(int(requested_count), 1), len(question_pool), self.MAX_QUESTIONS_PER_QUIZ)
            questions = question_pool[:actual_count]

        self.sessions[user_id] = {
            "session_id": secrets.token_urlsafe(12),
            "user_id": user_id,
            "set_id": set_id,
            "set_title": set_item.get("title") or "Quiz Set",
            "requested_count": actual_count,
            "questions": questions,
            "index": 0,
            "current_question_started_at": time.time(),
            "started_at": self._timestamp_now(),
            "locked": False,
            "awaiting_next": False,
            "last_result": None,
            "correct_count": 0,
            "wrong_count": 0,
            "skipped_count": 0,
            "responses": [],
            "stats_started": False,
            "last_processed_key": None,
            "pause_count": 0,
            "is_paused": False,
            "paused_at": None,
            "paused_remaining_seconds": None,
            "max_breaks": int(settings.get("max_breaks") or 0),
            "bonus_seconds": 0,
        }
        self._persist_session(user_id)
        self.logger.info(
            "Quiz start | user_id=%s set_id=%s requested_count=%s actual_count=%s exam_title=%s set_title=%s force_full_set=%s",
            user_id,
            set_id,
            requested_count,
            actual_count,
            exam_item.get("title") if exam_item else None,
            set_item.get("title"),
            use_full_set,
        )
        return self.sessions[user_id], None

    def get_session(self, user_id: int) -> dict | None:
        session = self.sessions.get(user_id)
        if session:
            return session
        restored = self._load_persisted_session(user_id)
        if restored:
            self.sessions[user_id] = restored
            self.logger.info(
                "Quiz session restored from database | user_id=%s set_id=%s index=%s",
                user_id,
                restored.get("set_id"),
                restored.get("index"),
            )
        return restored

    def build_session_reference(self, user_id: int) -> dict | None:
        session = self.get_session(user_id)
        if not session:
            return None
        return {
            "session_id": session.get("session_id") or "",
            "user_id": int(session.get("user_id") or user_id),
            "set_id": int(session.get("set_id") or 0),
        }

    def load_completed_summary(self, user_id: int) -> dict | None:
        payload = self._load_persisted_payload(user_id)
        if not payload:
            return None
        summary = payload.get("completed_summary")
        return deepcopy(summary) if isinstance(summary, dict) else None

    def build_session_snapshot(self, user_id: int) -> dict | None:
        quiz_session = self.get_session(user_id)
        if not quiz_session:
            return None
        settings = quiz_settings_service.get_settings()

        current_question = None
        index = int(quiz_session.get("index") or 0)
        questions = quiz_session.get("questions") or []
        if quiz_session.get("awaiting_next") and quiz_session.get("last_result"):
            current_question = self._result_question_payload(quiz_session)
        elif 0 <= index < len(questions):
            current_question = self.get_current_question(user_id)

        return {
            "set_id": quiz_session.get("set_id"),
            "set_title": quiz_session.get("set_title"),
            "requested_count": quiz_session.get("requested_count"),
            "index": index,
            "total": len(questions),
            "started_at": quiz_session.get("started_at"),
            "locked": bool(quiz_session.get("locked")),
            "awaiting_next": bool(quiz_session.get("awaiting_next")),
            "correct_count": int(quiz_session.get("correct_count") or 0),
            "wrong_count": int(quiz_session.get("wrong_count") or 0),
            "skipped_count": int(quiz_session.get("skipped_count") or 0),
            "last_result": deepcopy(quiz_session.get("last_result")),
            "responses": deepcopy(quiz_session.get("responses") or []),
            "current_question": deepcopy(current_question),
            "stats_started": bool(quiz_session.get("stats_started")),
            "pause_count": int(quiz_session.get("pause_count") or 0),
            "is_paused": bool(quiz_session.get("is_paused")),
            "allow_resume": bool(settings.get("allow_resume", True)),
            "max_breaks": int(quiz_session.get("max_breaks") or settings.get("max_breaks") or 0),
            "too_many_breaks": self.has_exceeded_break_limit(quiz_session, max_breaks=int(quiz_session.get("max_breaks") or settings.get("max_breaks") or 0)),
            "bonus_seconds": min(max(int(quiz_session.get("bonus_seconds") or 0), 0), self.MAX_BONUS_SECONDS),
        }

    def get_current_question(self, user_id: int) -> dict | None:
        session = self.get_session(user_id)
        if not session:
            return None
        if session.get("awaiting_next"):
            return self._result_question_payload(session)
        questions = session.get("questions") or []
        index = int(session.get("index") or 0)
        if index < 0 or index >= len(questions):
            return None
        question = deepcopy(questions[index])
        question["current_bonus_seconds"] = min(max(int(session.get("bonus_seconds") or 0), 0), self.MAX_BONUS_SECONDS)
        question["allowed_seconds"] = self._question_allowed_seconds(question, bonus_seconds=question["current_bonus_seconds"])
        question["remaining_seconds"] = self.remaining_seconds(user_id)
        question["number"] = index + 1
        question["total"] = len(questions)
        return question

    def pause_quiz(self, user_id: int) -> dict | None:
        session = self.get_session(user_id)
        if not session:
            return None

        settings = quiz_settings_service.get_settings()
        if not bool(settings.get("allow_resume", True)):
            return {"ok": False, "message": "Quiz resume is currently disabled by the admin."}
        if session.get("awaiting_next"):
            return {"ok": False, "message": "You can pause only while a live question is active."}
        if session.get("is_paused"):
            return {"ok": True, "message": "This quiz is already paused."}

        session["pause_count"] = int(session.get("pause_count") or 0) + 1
        session["is_paused"] = True
        session["paused_at"] = self._timestamp_now()
        session["paused_remaining_seconds"] = self.remaining_seconds(user_id)
        self._persist_session(user_id)
        max_breaks = int(session.get("max_breaks") or settings.get("max_breaks") or 0)
        session["max_breaks"] = max_breaks
        return {
            "ok": True,
            "pause_count": int(session.get("pause_count") or 0),
            "warning": self.BREAK_WARNING_MESSAGE if self.has_exceeded_break_limit(session, max_breaks=max_breaks) else None,
        }

    def resume_quiz(self, user_id: int, set_id: int | None = None) -> dict | None:
        session = self.get_session(user_id)
        if not session:
            return None
        if set_id is not None and int(session.get("set_id") or 0) != int(set_id):
            return None
        if not session.get("is_paused"):
            return {"ok": True, "warning": None}

        questions = session.get("questions") or []
        index = int(session.get("index") or 0)
        question = questions[index] if 0 <= index < len(questions) else None
        bonus_seconds = min(max(int(session.get("bonus_seconds") or 0), 0), self.MAX_BONUS_SECONDS)
        time_limit = self._question_allowed_seconds(question or {}, bonus_seconds=bonus_seconds)
        paused_remaining = int(session.get("paused_remaining_seconds") or 0)
        elapsed = max(time_limit - paused_remaining, 0)
        session["current_question_started_at"] = time.time() - elapsed
        session["is_paused"] = False
        session["paused_at"] = None
        session["paused_remaining_seconds"] = None
        self._persist_session(user_id)
        max_breaks = int(session.get("max_breaks") or quiz_settings_service.get_settings().get("max_breaks") or 0)
        session["max_breaks"] = max_breaks
        return {
            "ok": True,
            "warning": self.BREAK_WARNING_MESSAGE if self.has_exceeded_break_limit(session, max_breaks=max_breaks) else None,
        }

    def answer_question(self, user_id: int, selected_index: int | None, action: str = "answer") -> dict | None:
        session = self.get_session(user_id)
        if not session:
            return None

        questions = session.get("questions") or []
        current_index = int(session.get("index") or 0)
        if current_index < 0:
            current_index = 0
            session["index"] = current_index
        if not questions or current_index > len(questions):
            self.logger.warning(
                "Quiz answer rejected due to corrupt session | user_id=%s index=%s question_count=%s action=%s",
                user_id,
                current_index,
                len(questions),
                action,
            )
            self.clear_session(user_id)
            return None

        if session.get("awaiting_next") and session.get("last_result"):
            self.logger.info(
                "Quiz duplicate answer ignored | user_id=%s index=%s action=%s",
                user_id,
                current_index,
                action,
            )
            return deepcopy(session.get("last_result"))

        question = self.get_current_question(user_id)
        if not question or session["locked"] or session.get("is_paused"):
            if not question:
                self.logger.warning(
                    "Quiz answer ignored without active question | user_id=%s index=%s action=%s locked=%s",
                    user_id,
                    current_index,
                    action,
                    bool(session.get("locked")),
                )
            return None

        question_key = f"{question['question_id']}:{current_index}"
        if session.get("last_processed_key") == question_key and session.get("last_result"):
            self.logger.info(
                "Quiz duplicate submit ignored | user_id=%s question_id=%s index=%s action=%s",
                user_id,
                question.get("question_id"),
                current_index,
                action,
            )
            return deepcopy(session.get("last_result"))

        result = {
            "action": action,
            "correct": False,
            "selected_index": selected_index,
            "correct_index": question["correct_index"],
            "correct_answer": question["correct_answer"],
            "explanation": question.get("explanation"),
            "question_id": question["question_id"],
            "question_text": question["question_text"],
            "image_path": question.get("image_path"),
            "number": question["number"],
            "answered_index": current_index,
            "next_index": min(current_index + 1, len(session.get("questions") or [])),
            "options": deepcopy(question["options"]),
            "time_limit": int(question.get("time_limit") or DEFAULT_QUESTION_TIME),
            "allowed_seconds": int(question.get("allowed_seconds") or self._question_allowed_seconds(question)),
            "current_bonus_seconds": int(question.get("current_bonus_seconds") or 0),
            "remaining_seconds": self.remaining_seconds(user_id),
        }

        if not session.get("stats_started"):
            user_service.record_quiz_start(user_id)
            session["stats_started"] = True

        if action == "answer" and selected_index is not None and 0 <= selected_index < len(question["options"]):
            selected_option = question["options"][selected_index]
            result["selected_text"] = selected_option["text"]
            if selected_option["text"].strip().lower() == question["correct_answer"].strip().lower():
                session["correct_count"] += 1
                user_service.record_answer(user_id, True)
                result["correct"] = True
            else:
                session["wrong_count"] += 1
                user_service.record_answer(user_id, False)
        else:
            session["skipped_count"] += 1
            result["action"] = "skip" if action == "skip" else "timeout"
            result["selected_text"] = None

        time_taken_seconds = max(result["allowed_seconds"] - result["remaining_seconds"], 0)
        earned_bonus = (
            self.BONUS_INCREMENT_SECONDS
            if result["correct"] and time_taken_seconds < (result["allowed_seconds"] / 2)
            else 0
        )
        next_bonus_seconds = min(
            max(int(session.get("bonus_seconds") or 0), 0) + earned_bonus,
            self.MAX_BONUS_SECONDS,
        )
        if not result["correct"]:
            next_bonus_seconds = min(max(int(session.get("bonus_seconds") or 0), 0), self.MAX_BONUS_SECONDS)
        session["bonus_seconds"] = next_bonus_seconds
        result["time_taken_seconds"] = time_taken_seconds
        result["bonus_awarded_seconds"] = earned_bonus
        result["next_bonus_seconds"] = next_bonus_seconds
        result["earned_fast_bonus"] = earned_bonus > 0

        session["locked"] = True
        session["awaiting_next"] = True
        session["last_result"] = result
        session["last_processed_key"] = question_key
        session.setdefault("responses", []).append(self._review_payload(result))
        session["index"] = result["next_index"]
        self._persist_session(user_id)
        self.logger.info(
            "Quiz answer submit | user_id=%s question_id=%s index=%s action=%s selected_index=%s correct=%s",
            user_id,
            result.get("question_id"),
            current_index,
            result.get("action"),
            selected_index,
            result.get("correct"),
        )
        return result

    def next_question(self, user_id: int) -> bool:
        session = self.get_session(user_id)
        if not session:
            self.logger.warning("Quiz next question requested without active session | user_id=%s", user_id)
            return False

        next_index = int(session.get("index") or 0)
        if not session.get("awaiting_next"):
            next_index = min(next_index + 1, len(session.get("questions") or []))
            session["index"] = next_index

        session["locked"] = False
        session["awaiting_next"] = False
        session["last_result"] = None
        session["last_processed_key"] = None
        session["current_question_started_at"] = time.time()
        session["is_paused"] = False
        session["paused_at"] = None
        session["paused_remaining_seconds"] = None
        has_more = next_index < len(session.get("questions") or [])
        if has_more:
            self._persist_session(user_id)
        else:
            self._delete_persisted_session(user_id)
        self.logger.info(
            "Quiz next question | user_id=%s set_id=%s next_index=%s total=%s has_more=%s",
            user_id,
            session.get("set_id"),
            next_index,
            len(session.get("questions") or []),
            has_more,
        )
        return has_more

    def submit_quiz(self, user_id: int, ended_reason: str = "submitted") -> dict:
        session = self.get_session(user_id)
        if not session:
            return self._summary_payload(0, 0, 0, None, 0, [], ended_reason, None, None)

        summary = self._summary_payload(
            session["correct_count"],
            session["wrong_count"],
            session["skipped_count"],
            session["set_id"],
            len(session["questions"]),
            deepcopy(session.get("responses", [])),
            ended_reason,
            session.get("started_at"),
            self._timestamp_now(),
        )
        summary["set_title"] = session.get("set_title")
        self.logger.info(
            "Quiz submit | user_id=%s set_id=%s correct=%s wrong=%s skipped=%s ended_reason=%s",
            user_id,
            session.get("set_id"),
            summary["correct"],
            summary["wrong"],
            summary["skipped"],
            ended_reason,
        )
        user_service.record_quiz_attempt(
            user_id=user_id,
            set_id=session["set_id"],
            requested_count=len(session["questions"]),
            correct_count=summary["correct"],
            wrong_count=summary["wrong"],
            skipped_count=summary["skipped"],
            ended_reason=ended_reason,
        )
        self._persist_completed_summary(user_id, session, summary)
        self.sessions.pop(user_id, None)
        return summary

    def clear_session(self, user_id: int) -> None:
        self.sessions.pop(user_id, None)
        self._delete_persisted_session(user_id)

    def remaining_seconds(self, user_id: int) -> int:
        session = self.get_session(user_id)
        if not session:
            return 0
        if session.get("awaiting_next"):
            return 0
        if session.get("is_paused"):
            return max(0, int(session.get("paused_remaining_seconds") or 0))
        questions = session.get("questions") or []
        index = int(session.get("index") or 0)
        if index < 0 or index >= len(questions):
            return 0
        question = questions[index]
        elapsed = int(time.time() - float(session.get("current_question_started_at") or time.time()))
        allowed_seconds = self._question_allowed_seconds(
            question,
            bonus_seconds=min(max(int(session.get("bonus_seconds") or 0), 0), self.MAX_BONUS_SECONDS),
        )
        return max(0, allowed_seconds - elapsed)

    def user_performance_snapshot(self, user_id: int, limit: int = 8) -> dict:
        user = user_service.get_user(user_id)
        attempts = self.user_attempt_history(user_id, limit=limit)
        completed_attempts = [item for item in attempts if item.get("ended_reason") == "completed"]
        best_score = max((float(item.get("score") or 0) for item in attempts), default=0.0)
        average_score = (
            sum(float(item.get("score") or 0) for item in attempts) / len(attempts)
            if attempts
            else 0.0
        )
        return {
            "user": user,
            "recent_attempts": attempts,
            "completed_attempts": len(completed_attempts),
            "best_score": best_score,
            "average_score": average_score,
        }

    def user_attempt_history(self, user_id: int, limit: int = 8) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    qa.*,
                    s.title AS set_title,
                    e.title AS exam_title
                FROM quiz_attempts qa
                LEFT JOIN exam_sets s ON s.set_id = qa.set_id
                LEFT JOIN exams e ON e.exam_id = s.exam_id
                WHERE qa.user_id = ?
                ORDER BY qa.created_at DESC, qa.attempt_id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()

        attempts = []
        for row in rows:
            item = dict(row)
            attempted = int(item.get("correct_count") or 0) + int(item.get("wrong_count") or 0)
            score = float(item.get("correct_count") or 0) - (float(item.get("wrong_count") or 0) * 0.25)
            accuracy = (float(item.get("correct_count") or 0) / attempted * 100) if attempted else 0.0
            attempts.append(
                {
                    **item,
                    "attempted": attempted,
                    "completed": attempted + int(item.get("skipped_count") or 0),
                    "score": score,
                    "accuracy": accuracy,
                    "progress_percent": (
                        ((attempted + int(item.get("skipped_count") or 0)) / int(item.get("requested_count") or 0) * 100)
                        if int(item.get("requested_count") or 0)
                        else 0.0
                    ),
                }
            )
        return attempts

    def count_attempts_for_set(self, user_id: int, set_id: int) -> int:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM quiz_attempts WHERE user_id = ? AND set_id = ?",
                (user_id, set_id),
            ).fetchone()
        return int((row["count"] if row else 0) or 0)

    def has_exceeded_break_limit(self, session: dict | None, *, max_breaks: int | None = None) -> bool:
        if not session:
            return False
        resolved_max_breaks = max_breaks
        if resolved_max_breaks is None:
            resolved_max_breaks = int(session.get("max_breaks") or 0)
        return int(session.get("pause_count") or 0) > int(resolved_max_breaks or 0)

    def leaderboard_for_set(self, set_id: int, *, current_user_id: int | None = None) -> dict:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    qa.*,
                    u.full_name,
                    u.is_premium
                FROM quiz_attempts qa
                JOIN users u ON u.user_id = qa.user_id
                WHERE qa.set_id = ?
                ORDER BY qa.created_at DESC, qa.attempt_id DESC
                """,
                (set_id,),
            ).fetchall()

        best_attempts: dict[int, dict] = {}
        for row in rows:
            item = self._attempt_with_metrics(dict(row))
            attempt_user_id = int(item.get("user_id") or 0)
            current = best_attempts.get(attempt_user_id)
            if current is None or self._attempt_rank_key(item) > self._attempt_rank_key(current):
                best_attempts[attempt_user_id] = item

        ordered = sorted(best_attempts.values(), key=self._attempt_rank_key, reverse=True)
        top_ten = [
            self._serialize_leaderboard_row(item, index, current_user_id)
            for index, item in enumerate(ordered[:10], start=1)
        ]

        current_rank = None
        current_index = None
        for index, item in enumerate(ordered, start=1):
            if current_user_id is not None and int(item.get("user_id") or 0) == int(current_user_id):
                current_rank = index
                current_index = index - 1
                break

        rank_zone: list[dict] = []
        if current_index is not None:
            start = max(current_index - 4, 0)
            end = min(start + 10, len(ordered))
            start = max(end - 10, 0)
            rank_zone = [
                self._serialize_leaderboard_row(item, index, current_user_id)
                for index, item in enumerate(ordered[start:end], start=start + 1)
            ]

        return {
            "top_ten": top_ten,
            "rank_zone": rank_zone,
            "current_rank": current_rank,
        }

    def submit_question_report(self, *, user_id: int, question_id: int, set_id: int, reason: str) -> int:
        cleaned_reason = " ".join(str(reason or "").strip().split())
        if len(cleaned_reason) < 5:
            raise ValueError("Please add a short reason with at least 5 characters.")

        with database.connection() as conn:
            question_row = conn.execute(
                "SELECT question_id, set_id FROM questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()
            if not question_row:
                raise ValueError("This question could not be found anymore.")
            if int(question_row["set_id"]) != int(set_id):
                raise ValueError("Question report could not be matched to this quiz set.")

            cursor = conn.execute(
                """
                INSERT INTO question_reports (user_id, question_id, set_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, question_id, set_id, cleaned_reason, self._timestamp_now()),
            )

        report_id = int(cursor.lastrowid)
        self.logger.info(
            "Question reported | report_id=%s user_id=%s question_id=%s set_id=%s",
            report_id,
            user_id,
            question_id,
            set_id,
        )
        return report_id


    def _ensure_active_session_table(self, conn) -> None:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.ACTIVE_SESSION_TABLE} (
                user_id BIGINT PRIMARY KEY,
                session_payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _persist_session(self, user_id: int) -> None:
        session = self.sessions.get(user_id)
        if not session:
            return
        self._persist_payload(user_id, session)

    def _persist_completed_summary(self, user_id: int, session: dict, summary: dict) -> None:
        payload = {
            "session_id": session.get("session_id") or secrets.token_urlsafe(12),
            "user_id": int(session.get("user_id") or user_id),
            "set_id": int(session.get("set_id") or 0),
            "set_title": session.get("set_title") or summary.get("set_title") or "Quiz Set",
            "completed_summary": summary,
        }
        self._persist_payload(user_id, payload)

    def _persist_payload(self, user_id: int, payload_data: dict) -> None:
        payload = json.dumps(payload_data, ensure_ascii=False, separators=(",", ":"))
        updated_at = self._timestamp_now()
        with database.connection() as conn:
            self._ensure_active_session_table(conn)
            conn.execute(f"DELETE FROM {self.ACTIVE_SESSION_TABLE} WHERE user_id = ?", (user_id,))
            conn.execute(
                f"INSERT INTO {self.ACTIVE_SESSION_TABLE} (user_id, session_payload, updated_at) VALUES (?, ?, ?)",
                (user_id, payload, updated_at),
            )

    def _load_persisted_payload(self, user_id: int) -> dict | None:
        with database.connection() as conn:
            self._ensure_active_session_table(conn)
            row = conn.execute(
                f"SELECT session_payload FROM {self.ACTIVE_SESSION_TABLE} WHERE user_id = ?",
                (user_id,),
            ).fetchone()

        if not row:
            return None

        try:
            return json.loads(row["session_payload"] if isinstance(row, dict) else row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            self.logger.exception("Stored quiz session payload could not be decoded | user_id=%s", user_id)
            self._delete_persisted_session(user_id)
            return None

    def _load_persisted_session(self, user_id: int) -> dict | None:
        payload = self._load_persisted_payload(user_id)
        if not payload:
            return None

        session = self._normalize_loaded_session(payload)
        if not session:
            return None
        return session

    def _delete_persisted_session(self, user_id: int) -> None:
        with database.connection() as conn:
            self._ensure_active_session_table(conn)
            conn.execute(f"DELETE FROM {self.ACTIVE_SESSION_TABLE} WHERE user_id = ?", (user_id,))

    def _normalize_loaded_session(self, payload: dict) -> dict | None:
        if not isinstance(payload, dict):
            return None

        questions = payload.get("questions")
        responses = payload.get("responses")
        if not isinstance(questions, list) or not questions:
            return None
        if responses is None or not isinstance(responses, list):
            responses = []

        try:
            normalized = {
                "session_id": payload.get("session_id") or secrets.token_urlsafe(12),
                "user_id": int(payload.get("user_id") or 0),
                "set_id": int(payload.get("set_id") or 0),
                "set_title": payload.get("set_title") or "Quiz Set",
                "requested_count": int(payload.get("requested_count") or len(questions)),
                "questions": questions,
                "index": int(payload.get("index") or 0),
                "current_question_started_at": float(payload.get("current_question_started_at") or time.time()),
                "started_at": payload.get("started_at") or self._timestamp_now(),
                "locked": bool(payload.get("locked")),
                "awaiting_next": bool(payload.get("awaiting_next")),
                "last_result": payload.get("last_result"),
                "correct_count": int(payload.get("correct_count") or 0),
                "wrong_count": int(payload.get("wrong_count") or 0),
                "skipped_count": int(payload.get("skipped_count") or 0),
                "responses": responses,
                "stats_started": bool(payload.get("stats_started")),
                "last_processed_key": payload.get("last_processed_key"),
                "pause_count": int(payload.get("pause_count") or 0),
                "is_paused": bool(payload.get("is_paused")),
                "paused_at": payload.get("paused_at"),
                "paused_remaining_seconds": (
                    int(payload.get("paused_remaining_seconds"))
                    if payload.get("paused_remaining_seconds") is not None
                    else None
                ),
                "max_breaks": int(payload.get("max_breaks") or 0),
                "bonus_seconds": min(max(int(payload.get("bonus_seconds") or 0), 0), self.MAX_BONUS_SECONDS),
            }
        except (TypeError, ValueError):
            return None

        if normalized["index"] < 0:
            normalized["index"] = 0
        if normalized["index"] > len(questions):
            normalized["index"] = len(questions)
        return normalized

    def _result_question_payload(self, session: dict) -> dict | None:
        result = session.get("last_result") or {}
        questions = session.get("questions") or []
        if not result:
            return None
        answered_index = int(result.get("answered_index") or 0)
        return {
            "question_id": result.get("question_id"),
            "question_text": result.get("question_text") or "",
            "image_path": result.get("image_path"),
            "options": deepcopy(result.get("options") or []),
            "correct_answer": result.get("correct_answer"),
            "correct_index": int(result.get("correct_index") or 0),
            "remaining_seconds": 0,
            "time_limit": int(result.get("time_limit") or DEFAULT_QUESTION_TIME),
            "allowed_seconds": int(result.get("allowed_seconds") or DEFAULT_QUESTION_TIME),
            "current_bonus_seconds": int(result.get("current_bonus_seconds") or 0),
            "number": answered_index + 1,
            "total": len(questions),
        }

    def _should_force_full_set(self, exam_item: dict | None, set_item: dict | None) -> bool:
        if not exam_item or not set_item:
            return False
        return (str(exam_item.get("title") or "").strip().upper() == self.FULL_SET_EXAM_TITLE and str(set_item.get("title") or "").strip() == self.FULL_SET_TITLE)

    def _prepare_question(self, question: dict) -> dict:
        item = deepcopy(question)
        item["time_limit"] = int(item.get("time_limit") or DEFAULT_QUESTION_TIME)
        options = [{"id": f"opt_{index}", "text": value} for index, value in enumerate(item["options"])]
        random.shuffle(options)
        item["options"] = options
        item["correct_answer"] = str(item["correct_option"]).strip()
        item["correct_index"] = next(
            (index for index, option in enumerate(options) if option["text"].strip().lower() == item["correct_answer"].lower()),
            0,
        )
        return item

    def _question_allowed_seconds(self, question: dict | None, bonus_seconds: int | None = None) -> int:
        base_time = int((question or {}).get("time_limit") or DEFAULT_QUESTION_TIME)
        normalized_bonus = min(max(int(bonus_seconds or 0), 0), self.MAX_BONUS_SECONDS)
        return base_time + normalized_bonus

    def _summary_payload(
        self,
        correct: int,
        wrong: int,
        skipped: int,
        set_id: int | None,
        requested_count: int,
        review_items: list[dict],
        ended_reason: str,
        started_at: str | None,
        completed_at: str | None,
    ) -> dict:
        attempted = correct + wrong
        score = correct - (wrong * 0.25)
        accuracy = (correct / attempted * 100) if attempted else 0.0
        completed = attempted + skipped
        progress_percent = (completed / requested_count * 100) if requested_count else 0.0
        return {
            "correct": correct,
            "wrong": wrong,
            "skipped": skipped,
            "attempted": attempted,
            "completed": completed,
            "score": score,
            "accuracy": accuracy,
            "progress_percent": progress_percent,
            "negative_marking": wrong * 0.25,
            "set_id": set_id,
            "requested_count": requested_count,
            "review_items": review_items,
            "ended_reason": ended_reason,
            "started_at": started_at,
            "completed_at": completed_at,
        }

    def _review_payload(self, result: dict) -> dict:
        return {
            "number": result["number"],
            "question_id": result["question_id"],
            "question_text": result["question_text"],
            "action": result["action"],
            "correct": result["correct"],
            "selected_index": result.get("selected_index"),
            "selected_text": result.get("selected_text"),
            "correct_index": result["correct_index"],
            "correct_answer": result["correct_answer"],
            "explanation": result.get("explanation"),
            "image_path": result.get("image_path"),
            "options": deepcopy(result.get("options") or []),
            "time_limit": result.get("time_limit"),
            "allowed_seconds": result.get("allowed_seconds"),
            "remaining_seconds": result.get("remaining_seconds"),
            "current_bonus_seconds": result.get("current_bonus_seconds"),
            "bonus_awarded_seconds": result.get("bonus_awarded_seconds"),
            "next_bonus_seconds": result.get("next_bonus_seconds"),
            "time_taken_seconds": result.get("time_taken_seconds"),
        }

    def _timestamp_now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def _attempt_with_metrics(self, item: dict) -> dict:
        attempted = int(item.get("correct_count") or 0) + int(item.get("wrong_count") or 0)
        skipped = int(item.get("skipped_count") or 0)
        requested_count = int(item.get("requested_count") or 0)
        completed = attempted + skipped
        return {
            **item,
            "attempted": attempted,
            "completed": completed,
            "score": float(item.get("correct_count") or 0) - (float(item.get("wrong_count") or 0) * 0.25),
            "accuracy": (float(item.get("correct_count") or 0) / attempted * 100) if attempted else 0.0,
            "progress_percent": (completed / requested_count * 100) if requested_count else 0.0,
        }

    def _attempt_rank_key(self, item: dict) -> tuple:
        return (
            float(item.get("score") or 0),
            float(item.get("accuracy") or 0),
            int(item.get("correct_count") or 0),
            -int(item.get("wrong_count") or 0),
            str(item.get("created_at") or ""),
            int(item.get("attempt_id") or 0),
        )

    def _serialize_leaderboard_row(self, item: dict, rank: int, current_user_id: int | None) -> dict:
        return {
            **item,
            "rank": rank,
            "rank_display": f"#{rank}",
            "full_name": item.get("full_name") or f"User {item.get('user_id')}",
            "is_current_user": current_user_id is not None and int(item.get("user_id") or 0) == int(current_user_id),
        }

    def build_summary_display_data(self, summary: dict, user: dict | None) -> dict:
        item = dict(summary or {})
        item["started_at_display"] = format_user_datetime(item.get("started_at"), user)
        item["completed_at_display"] = format_user_datetime(item.get("completed_at"), user)
        return item


web_quiz_service = WebQuizService()








