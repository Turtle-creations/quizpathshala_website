from copy import deepcopy
from datetime import datetime, timezone
import json
import random
import time

from config import DEFAULT_QUESTION_TIME
from db.database import database
from services.exam_service_db import exam_service
from services.premium_service_db import premium_service
from services.user_service_db import user_service
from utils.logging_utils import get_logger


class WebQuizService:
    QUIZ_COUNT_OPTIONS = (20, 50, 100)
    MAX_QUESTIONS_PER_QUIZ = 100
    SESSION_STORAGE_KEY = "active_quiz_session"
    ACTIVE_SESSION_TABLE = "web_quiz_sessions"

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

        locked = bool(int(set_item.get("is_premium_locked", 0)))
        if locked and not (premium_service.is_premium(user_id) or user_service.is_admin(user_id)):
            return None, "This quiz set is premium-only."
        if int(requested_count) not in self.QUIZ_COUNT_OPTIONS:
            return None, "Please choose 20, 50, or 100 questions."

        question_pool = [self._prepare_question(item) for item in exam_service.get_questions(set_id)]
        if not question_pool:
            return None, "No questions are available in this set yet."

        actual_count = min(max(int(requested_count), 1), len(question_pool), self.MAX_QUESTIONS_PER_QUIZ)
        random.shuffle(question_pool)
        questions = question_pool[:actual_count]

        self.sessions[user_id] = {
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
        }
        self._persist_session(user_id)
        self.logger.info(
            "Quiz start | user_id=%s set_id=%s requested_count=%s actual_count=%s",
            user_id,
            set_id,
            requested_count,
            actual_count,
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

    def build_session_snapshot(self, user_id: int) -> dict | None:
        quiz_session = self.get_session(user_id)
        if not quiz_session:
            return None

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
        question["remaining_seconds"] = self.remaining_seconds(user_id)
        question["number"] = index + 1
        question["total"] = len(questions)
        return question

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
        if not question or session["locked"]:
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
        self.sessions.pop(user_id, None)
        self._delete_persisted_session(user_id)
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
        questions = session.get("questions") or []
        index = int(session.get("index") or 0)
        if index < 0 or index >= len(questions):
            return 0
        question = questions[index]
        elapsed = int(time.time() - float(session.get("current_question_started_at") or time.time()))
        return max(0, int(question["time_limit"]) - elapsed)

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
                    "score": score,
                    "accuracy": accuracy,
                }
            )
        return attempts


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

        payload = json.dumps(session, ensure_ascii=False, separators=(",", ":"))
        updated_at = self._timestamp_now()
        with database.connection() as conn:
            self._ensure_active_session_table(conn)
            conn.execute(f"DELETE FROM {self.ACTIVE_SESSION_TABLE} WHERE user_id = ?", (user_id,))
            conn.execute(
                f"INSERT INTO {self.ACTIVE_SESSION_TABLE} (user_id, session_payload, updated_at) VALUES (?, ?, ?)",
                (user_id, payload, updated_at),
            )

    def _load_persisted_session(self, user_id: int) -> dict | None:
        with database.connection() as conn:
            self._ensure_active_session_table(conn)
            row = conn.execute(
                f"SELECT session_payload FROM {self.ACTIVE_SESSION_TABLE} WHERE user_id = ?",
                (user_id,),
            ).fetchone()

        if not row:
            return None

        try:
            payload = json.loads(row["session_payload"] if isinstance(row, dict) else row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            self.logger.exception("Stored quiz session payload could not be decoded | user_id=%s", user_id)
            self._delete_persisted_session(user_id)
            return None

        session = self._normalize_loaded_session(payload)
        if not session:
            self._delete_persisted_session(user_id)
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
            "number": answered_index + 1,
            "total": len(questions),
        }

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
        return {
            "correct": correct,
            "wrong": wrong,
            "skipped": skipped,
            "attempted": attempted,
            "score": score,
            "accuracy": accuracy,
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
            "remaining_seconds": result.get("remaining_seconds"),
        }

    def _timestamp_now(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


web_quiz_service = WebQuizService()
