import os
import tempfile
import time
import unittest
from pathlib import Path


TEST_EMAIL_PREFIX = "codex-set-rename-bonus-test-"
TEST_DB_DIR = Path(tempfile.gettempdir()) / "quizpathshala_set_rename_bonus_tests"
TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
TEST_DB_PATH = TEST_DB_DIR / "quizpathshala_set_rename_bonus_test.db"

os.environ["APP_ENV"] = "test"
os.environ["DB_PATH"] = str(TEST_DB_PATH)

from config import DEFAULT_QUESTION_TIME
from db.database import database

database.backend = "sqlite"
database.dsn = str(TEST_DB_PATH)
database._initialized = False

from services.web_admin_service import web_admin_service
from services.web_identity_service import web_identity_service
from services.web_quiz_service import web_quiz_service
from services.user_service_db import user_service
from webhook_server import app


class AdminSetRenameAndQuizBonusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
        database.backend = "sqlite"
        database.dsn = str(TEST_DB_PATH)
        database._initialized = False
        database.initialize()

    def setUp(self):
        web_quiz_service.sessions.clear()
        with database.connection() as conn:
            web_quiz_service._ensure_active_session_table(conn)
            conn.execute("DELETE FROM web_quiz_sessions")
            conn.execute("DELETE FROM quiz_attempts")
            conn.execute("DELETE FROM questions")
            conn.execute("DELETE FROM exam_sets")
            conn.execute("DELETE FROM exams")
            conn.execute("DELETE FROM users WHERE login_identifier LIKE ?", (f"{TEST_EMAIL_PREFIX}%",))

    def _create_user(self, suffix: str, role: str = "user") -> dict:
        return user_service.upsert_login_account(
            login_identifier=f"{TEST_EMAIL_PREFIX}{suffix}@example.com",
            password="password123",
            full_name=f"User {suffix.title()}",
            role=role,
        )

    def _login(self, client, user: dict, *, admin: bool = False):
        snapshot = web_identity_service._build_user_snapshot(user)
        with client.session_transaction() as session:
            session[web_identity_service.AUTH_USER_KEY] = int(user["user_id"])
            session[web_identity_service.ROLE_KEY] = str(user.get("user_role") or "user")
            session[web_identity_service.SESSION_KEY] = int(user["user_id"])
            session[web_identity_service.SNAPSHOT_KEY] = snapshot
            if admin:
                session[web_identity_service.ADMIN_KEY] = True

    def _create_exam_with_sets(self, suffix: str, set_titles: list[str]) -> tuple[int, list[int]]:
        exam_result = web_admin_service.add_exam(
            title=f"Set Rename Exam {suffix}",
            description="Set rename test exam",
        )
        exam_id = int(exam_result["row_id"])
        set_ids: list[int] = []
        for index, title in enumerate(set_titles, start=1):
            set_result = web_admin_service.add_set(
                exam_id=exam_id,
                title=title,
                description=f"Set {index}",
            )
            set_ids.append(int(set_result["row_id"]))
        return exam_id, set_ids

    def _create_quiz_set(self, suffix: str, question_count: int) -> tuple[dict, int]:
        user = self._create_user(f"quiz-{suffix}")
        exam_result = web_admin_service.add_exam(
            title=f"Quiz Bonus Exam {suffix}",
            description="Quiz bonus test exam",
        )
        set_result = web_admin_service.add_set(
            exam_id=int(exam_result["row_id"]),
            title=f"Bonus Set {suffix}",
            description="Bonus coverage set",
        )
        set_id = int(set_result["row_id"])
        for number in range(1, question_count + 1):
            web_admin_service.add_question(
                set_id=set_id,
                question_text=(
                    f"In quiz {suffix}, what is the correct code for problem number {number} "
                    f"when the reference token is ITEM-{number * 7}?"
                ),
                options=[f"Correct {number}", f"Wrong A {number}", f"Wrong B {number}", f"Wrong C {number}"],
                correct_option=f"Correct {number}",
                allow_similar_duplicate=True,
            )
        return user, set_id

    def _start_quiz(self, user_id: int, set_id: int):
        session, error = web_quiz_service.start_quiz(user_id, set_id, 20)
        self.assertIsNone(error)
        self.assertIsNotNone(session)
        return session

    def _answer_current_question_fast_and_correct(self, user_id: int):
        question = web_quiz_service.get_current_question(user_id)
        self.assertIsNotNone(question)
        session = web_quiz_service.get_session(user_id)
        self.assertIsNotNone(session)
        session["current_question_started_at"] = time.time() - 4
        return web_quiz_service.answer_question(user_id, int(question["correct_index"]), action="answer")

    def test_admin_can_rename_set_from_set_management(self):
        admin_user = self._create_user("admin-rename", role="admin")
        exam_id, set_ids = self._create_exam_with_sets("rename", ["Practice Set", "Mock Set"])

        with app.test_client() as client:
            self._login(client, admin_user, admin=True)
            response = client.post(
                f"/admin/exams/{exam_id}/sets",
                data={
                    "action": "rename_set",
                    "set_id": str(set_ids[0]),
                    "title": "Renamed Practice Set",
                    "page": "1",
                    "return_anchor": "sets-section",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(web_admin_service.get_set_overview(set_ids[0])["title"], "Renamed Practice Set")
        self.assertIn("Set name updated successfully.", response.get_data(as_text=True))

    def test_admin_set_rename_rejects_empty_and_duplicate_names(self):
        admin_user = self._create_user("admin-validate", role="admin")
        exam_id, set_ids = self._create_exam_with_sets("validate", ["Alpha Set", "Beta Set"])

        with app.test_client() as client:
            self._login(client, admin_user, admin=True)
            empty_response = client.post(
                f"/admin/exams/{exam_id}/sets",
                data={
                    "action": "rename_set",
                    "set_id": str(set_ids[0]),
                    "title": "   ",
                    "page": "1",
                    "return_anchor": "sets-section",
                },
                follow_redirects=True,
            )
            duplicate_response = client.post(
                f"/admin/exams/{exam_id}/sets",
                data={
                    "action": "rename_set",
                    "set_id": str(set_ids[0]),
                    "title": " beta    set ",
                    "page": "1",
                    "return_anchor": "sets-section",
                },
                follow_redirects=True,
            )

        self.assertEqual(web_admin_service.get_set_overview(set_ids[0])["title"], "Alpha Set")
        self.assertIn("Set name is required.", empty_response.get_data(as_text=True))
        self.assertIn("A set with this name already exists in this exam.", duplicate_response.get_data(as_text=True))

    def test_default_timer_is_twenty_seconds(self):
        user, set_id = self._create_quiz_set("default", question_count=2)
        self._start_quiz(int(user["user_id"]), set_id)

        question = web_quiz_service.get_current_question(int(user["user_id"]))

        self.assertEqual(DEFAULT_QUESTION_TIME, 20)
        self.assertEqual(question["time_limit"], 20)
        self.assertEqual(question["current_bonus_seconds"], 0)
        self.assertEqual(question["allowed_seconds"], 20)

    def test_fast_correct_answer_adds_five_second_bonus_and_shows_in_play_ui(self):
        user, set_id = self._create_quiz_set("bonus-ui", question_count=3)
        user_id = int(user["user_id"])
        self._start_quiz(user_id, set_id)

        with app.test_client() as client:
            self._login(client, user)
            first_page = client.get("/play")
            self.assertIn("Current bonus +0 sec", first_page.get_data(as_text=True))

            result = self._answer_current_question_fast_and_correct(user_id)
            self.assertTrue(result["correct"])
            self.assertEqual(result["bonus_awarded_seconds"], 5)
            self.assertEqual(result["next_bonus_seconds"], 5)

            review_page = client.get("/play")
            review_html = review_page.get_data(as_text=True)
            self.assertIn("Fast answer bonus earned: +5 sec.", review_html)
            self.assertIn("Next question bonus: 5 sec.", review_html)

            advanced = web_quiz_service.next_question(user_id)
            self.assertTrue(advanced)
            next_page = client.get("/play")
            next_html = next_page.get_data(as_text=True)

        self.assertIn("Current bonus +5 sec", next_html)
        self.assertIn("Total 25 sec", next_html)
        next_question = web_quiz_service.get_current_question(user_id)
        self.assertEqual(next_question["allowed_seconds"], 25)

    def test_bonus_never_exceeds_sixty_seconds(self):
        user, set_id = self._create_quiz_set("bonus-cap", question_count=13)
        user_id = int(user["user_id"])
        self._start_quiz(user_id, set_id)

        for _ in range(12):
            result = self._answer_current_question_fast_and_correct(user_id)
            self.assertEqual(result["bonus_awarded_seconds"], 5)
            web_quiz_service.next_question(user_id)

        session = web_quiz_service.get_session(user_id)
        question = web_quiz_service.get_current_question(user_id)

        self.assertEqual(session["bonus_seconds"], 60)
        self.assertEqual(question["current_bonus_seconds"], 60)
        self.assertEqual(question["allowed_seconds"], 80)


if __name__ == "__main__":
    unittest.main()
