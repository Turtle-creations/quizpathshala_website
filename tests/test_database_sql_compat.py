import unittest

from db.database import Database


class DatabaseSqlCompatibilityTests(unittest.TestCase):
    def test_first_word_sql_uses_sqlite_functions_for_sqlite_backend(self):
        database = Database("sqlite", ":memory:")

        sql = database._first_word_sql("telegram_full_name")

        self.assertIn("instr(", sql)
        self.assertIn("substr(", sql)
        self.assertNotIn("POSITION(", sql)

    def test_first_word_sql_uses_postgres_functions_for_postgres_backend(self):
        database = Database("postgres", "postgresql://example")

        sql = database._first_word_sql("telegram_full_name")

        self.assertIn("POSITION(' ' IN telegram_full_name)", sql)
        self.assertIn("substring(telegram_full_name FROM 1 FOR POSITION(' ' IN telegram_full_name) - 1)", sql)
        self.assertNotIn("instr(", sql)


if __name__ == "__main__":
    unittest.main()
