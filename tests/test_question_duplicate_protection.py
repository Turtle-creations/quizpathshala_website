import os
import tempfile
import unittest
from pathlib import Path


TEST_EMAIL_PREFIX = "codex-question-duplicate-test-"
TEST_DB_DIR = Path(tempfile.gettempdir()) / "quizpathshala_question_duplicate_tests"
TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
TEST_DB_PATH = TEST_DB_DIR / "quizpathshala_question_duplicate_test.db"

os.environ["APP_ENV"] = "test"
os.environ["DB_PATH"] = str(TEST_DB_PATH)

from db.database import database

database.backend = "sqlite"
database.dsn = str(TEST_DB_PATH)
database._initialized = False

from services.exam_service_db import SimilarQuestionError, exam_service
from services.user_service_db import user_service
from services.web_admin_service import web_admin_service
from services.web_identity_service import web_identity_service
from webhook_server import app


class QuestionDuplicateProtectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
        database.backend = "sqlite"
        database.dsn = str(TEST_DB_PATH)
        database._initialized = False
        database.initialize()

    def setUp(self):
        with database.connection() as conn:
            conn.execute("DELETE FROM questions")
            conn.execute("DELETE FROM exam_sets")
            conn.execute("DELETE FROM exams")
            conn.execute("DELETE FROM users WHERE login_identifier LIKE ?", (f"{TEST_EMAIL_PREFIX}%",))

    def _create_admin_user(self, suffix: str) -> dict:
        return user_service.upsert_login_account(
            login_identifier=f"{TEST_EMAIL_PREFIX}{suffix}@example.com",
            password="password123",
            full_name=f"Admin {suffix.title()}",
            role="admin",
        )

    def _login(self, client, user: dict):
        snapshot = web_identity_service._build_user_snapshot(user)
        with client.session_transaction() as session:
            session[web_identity_service.AUTH_USER_KEY] = int(user["user_id"])
            session[web_identity_service.ROLE_KEY] = str(user.get("user_role") or "user")
            session[web_identity_service.SESSION_KEY] = int(user["user_id"])
            session[web_identity_service.SNAPSHOT_KEY] = snapshot
            session[web_identity_service.ADMIN_KEY] = True

    def _create_set(self, suffix: str) -> int:
        exam_result = web_admin_service.add_exam(title=f"Duplicate Test Exam {suffix}", description="Question duplicate coverage")
        set_result = web_admin_service.add_set(
            exam_id=int(exam_result["row_id"]),
            title=f"Duplicate Test Set {suffix}",
            description="Question duplicate coverage",
        )
        return int(set_result["row_id"])

    def test_save_question_blocks_same_set_duplicate_based_only_on_question_text(self):
        set_id = self._create_set("save")
        existing_question = (
            "In a right triangle, if one angle measures 90 degrees and another angle measures 35 degrees, "
            "what is the measure of the third angle?"
        )
        similar_question = (
            "In a right triangle, if one angle measures 90 degrees and another angle measures 35 degrees, "
            "what is the measure of the third angle!"
        )

        web_admin_service.add_question(
            set_id=set_id,
            question_text=existing_question,
            options=["45 degrees", "55 degrees", "65 degrees", "75 degrees"],
            correct_option="55 degrees",
        )

        with self.assertRaises(SimilarQuestionError) as context:
            exam_service.save_question(
                set_id=set_id,
                question_text=similar_question,
                options=["10 degrees", "20 degrees", "30 degrees", "40 degrees"],
                correct_option="20 degrees",
            )

        duplicate_details = context.exception.duplicate_details
        self.assertEqual(duplicate_details["message"], "Similar question already exists in this set")
        self.assertEqual(duplicate_details["matched_question"]["question_text"], existing_question)
        self.assertGreaterEqual(float(duplicate_details["matched_question"]["similarity_ratio"]), 0.98)

    def test_bulk_import_reports_similar_duplicate_details_even_when_options_change(self):
        set_id = self._create_set("bulk")
        existing_question = "Which planet is known as the Red Planet in our solar system and why is it famous for that color?"

        web_admin_service.add_question(
            set_id=set_id,
            question_text=existing_question,
            options=["Earth", "Mars", "Venus", "Jupiter"],
            correct_option="Mars",
        )

        duplicate_row = (
            "Which planet is known as the Red Planet in our solar system and why is it famous for that colour?"
            " | Mercury | Saturn | Neptune | Pluto | Saturn"
        )

        with self.assertRaises(SimilarQuestionError) as context:
            web_admin_service.bulk_import_questions(
                set_id=set_id,
                raw_text=duplicate_row,
            )

        duplicate_details = context.exception.duplicate_details
        self.assertEqual(duplicate_details["line_number"], 1)
        self.assertEqual(duplicate_details["line_text"], duplicate_row)
        self.assertEqual(duplicate_details["matched_question"]["question_text"], existing_question)
        self.assertGreaterEqual(float(duplicate_details["matched_question"]["similarity_ratio"]), 0.98)

    def test_admin_dashboard_duplicate_warning_shows_existing_question_preview(self):
        admin_user = self._create_admin_user("dashboard")
        set_id = self._create_set("dashboard")
        existing_question = "What is the chemical symbol for sodium in the periodic table and how is it written?"

        web_admin_service.add_question(
            set_id=set_id,
            question_text=existing_question,
            options=["So", "Sd", "Na", "No"],
            correct_option="Na",
        )

        with app.test_client() as client:
            self._login(client, admin_user)
            response = client.post(
                "/admin",
                data={
                    "action": "save_question",
                    "set_id": str(set_id),
                    "question_text": "What is the chemical symbol for sodium in the periodic table and how is it written!",
                    "option_a": "S",
                    "option_b": "N",
                    "option_c": "Na",
                    "option_d": "K",
                    "correct_option": "Na",
                    "explanation": "Sodium is written as Na.",
                    "time_limit": "20",
                    "return_anchor": "question-editor",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Similar question already exists in this set", page)
        self.assertIn(existing_question, page)
        self.assertIn("What is the chemical symbol for sodium in the periodic table and how is it written!", page)


if __name__ == "__main__":
    unittest.main()
