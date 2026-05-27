import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DB_DIR = Path(tempfile.gettempdir()) / "quizpathshala_health_tests"
TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
TEST_DB_PATH = TEST_DB_DIR / "quizpathshala_health_test.db"

os.environ["APP_ENV"] = "test"
os.environ["DB_PATH"] = str(TEST_DB_PATH)

if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()

from db.database import database

database.backend = "sqlite"
database.dsn = str(TEST_DB_PATH)
database._initialized = False

from webhook_server import app


class HealthEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if TEST_DB_PATH.exists():
            TEST_DB_PATH.unlink()
        database.backend = "sqlite"
        database.dsn = str(TEST_DB_PATH)
        database._initialized = False
        database.initialize()

    def test_health_returns_ok_without_html(self):
        with app.test_client() as client:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "OK")
        self.assertTrue(response.headers["Content-Type"].startswith("text/plain"))

    def test_head_root_skips_homepage_shared_context(self):
        with patch("routes.pages._shared_context", side_effect=AssertionError("homepage context should not run for HEAD /")):
            with app.test_client() as client:
                response = client.head("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "")


if __name__ == "__main__":
    unittest.main()
