from copy import deepcopy
from datetime import datetime, timezone
import random
import time

from config import DEFAULT_QUESTION_TIME
from db.database import database
from services.exam_service_db import exam_service
from services.premium_service_db import premium_service
from services.user_service_db import user_service


class WebQuizService:
    QUIZ_COUNT_OPTIONS = (20, 50, 100)
    MAX_QUESTIONS_PER_QUIZ = 100

    def __init__(self) -> None:
        self.sessions: dict[int, dict] = {}

    def list_exam_catalog(self, user_id: int) -> list[dict]:
        catalog = []
        for exam in exam_service.get_exams():
            sets = []
            for set_item in exam_service.get_sets(exam["exam_id"]):
                locked = bool(int(set_item.get("is_premium_locked", 0)))
                has_access = (not locked) or premium_service.is_premium(user_id) or user_service.is_admin(user_id)
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
        if not self.can_access_set(user_id, set_id):
            return None, "This quiz set is premium-only."
        if int(requested_count) not in self.QUIZ_COUNT_OPTIONS:
            return None, "Please choose 20, 50, or 100 questions."

        question_pool = [self._prepare_question(item) for item in exam_service.get_questions(set_id)]
        if not question_pool:
            return None, "No questions are available in this set yet."

        actual_count = min(max(int(requested_count), 1), len(question_pool), self.MAX_QUESTIONS_PER_QUIZ)
        random.shuffle(question_pool)
        questions = question_pool[:actual_count]
        set_item = exam_service.get_set(set_id) or {}

        self.sessions[user_id] = {
            "set_id": set_id,
            "set_title": set_item.get("title") or "Quiz Set",
            "requested_count": actual_count,
            "questions": questions,
            "index": 0,
            "current_question_started_at": time.time(),
            "started_at": self._timestamp_now(),
            "locked": False,
            "last_result": None,
            "correct_count": 0,
            "wrong_count": 0,
            "skipped_count": 0,
            "responses": [],
        }
        user_service.record_quiz_start(user_id)
        return self.sessions[user_id], None

    def get_session(self, user_id: int) -> dict | None:
        return self.sessions.get(user_id)

    def get_current_question(self, user_id: int) -> dict | None:
        session = self.get_session(user_id)
        if not session:
            return None
        index = session["index"]
        if index >= len(session["questions"]):
            return None
        question = deepcopy(session["questions"][index])
        question["remaining_seconds"] = self.remaining_seconds(user_id)
        question["number"] = index + 1
        question["total"] = len(session["questions"])
        return question

    def answer_question(self, user_id: int, selected_index: int | None, action: str = "answer") -> dict | None:
        session = self.get_session(user_id)
        question = self.get_current_question(user_id)
        if not session or not question or session["locked"]:
            return None

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
            "options": deepcopy(question["options"]),
            "time_limit": int(question.get("time_limit") or DEFAULT_QUESTION_TIME),
            "remaining_seconds": self.remaining_seconds(user_id),
        }

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
        session["last_result"] = result
        session.setdefault("responses", []).append(self._review_payload(result))
        return result

    def next_question(self, user_id: int) -> bool:
        session = self.get_session(user_id)
        if not session:
            return False
        session["index"] += 1
        session["locked"] = False
        session["last_result"] = None
        session["current_question_started_at"] = time.time()
        return session["index"] < len(session["questions"])

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
        return summary

    def remaining_seconds(self, user_id: int) -> int:
        session = self.get_session(user_id)
        if not session:
            return 0
        index = session["index"]
        if index >= len(session["questions"]):
            return 0
        question = session["questions"][index]
        elapsed = int(time.time() - session["current_question_started_at"])
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
