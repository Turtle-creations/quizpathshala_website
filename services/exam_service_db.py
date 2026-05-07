import json
from datetime import datetime
from functools import lru_cache

from config import DATA_DIR, DEFAULT_QUESTION_TIME
from db.database import database
from utils.logging_utils import get_logger


def timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


logger = get_logger(__name__)


def normalize_unicode_text(value: str | None) -> str | None:
    if value is None:
        return None

    text = str(value)
    if not text:
        return text

    suspicious_markers = ("\u00e0\u00a4", "\u00e0\u00a5", "\u00c3", "\u00c2", "\u00e2\u20ac", "\u00f0\u0178")
    if not any(marker in text for marker in suspicious_markers):
        return text

    try:
        repaired = text.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

    return repaired if repaired else text


class ExamService:
    def exam_storage_ready(self) -> bool:
        return database.tables_exist({"exams", "exam_sets", "questions"})

    def invalidate_cache(self):
        self.get_exams.cache_clear()
        self.get_sets.cache_clear()
        self.get_set.cache_clear()
        self.get_questions.cache_clear()

    @lru_cache(maxsize=1)
    def get_exams(self) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.exam_id,
                    e.title,
                    e.description,
                    COUNT(DISTINCT s.set_id) AS set_count,
                    COUNT(q.question_id) AS question_count
                FROM exams e
                LEFT JOIN exam_sets s ON s.exam_id = e.exam_id
                LEFT JOIN questions q ON q.set_id = s.set_id
                GROUP BY e.exam_id
                ORDER BY e.title
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @lru_cache(maxsize=64)
    def get_sets(self, exam_id: int) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.set_id,
                    s.exam_id,
                    s.title,
                    s.description,
                    s.is_premium_locked,
                    COUNT(q.question_id) AS question_count
                FROM exam_sets s
                LEFT JOIN questions q ON q.set_id = s.set_id
                WHERE s.exam_id = ?
                GROUP BY s.set_id
                ORDER BY s.title
                """,
                (exam_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    @lru_cache(maxsize=256)
    def get_set(self, set_id: int) -> dict | None:
        with database.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    s.set_id,
                    s.exam_id,
                    s.title,
                    s.description,
                    s.is_premium_locked,
                    COUNT(q.question_id) AS question_count
                FROM exam_sets s
                LEFT JOIN questions q ON q.set_id = s.set_id
                WHERE s.set_id = ?
                GROUP BY s.set_id
                """,
                (set_id,),
            ).fetchone()
        return dict(row) if row else None

    @lru_cache(maxsize=128)
    def get_questions(self, set_id: int) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM questions WHERE set_id = ? ORDER BY question_id",
                (set_id,),
            ).fetchall()

        questions = []
        for row in rows:
            item = self._normalize_question_record(dict(row))
            item["options"] = [
                item["option_a"],
                item["option_b"],
                item["option_c"],
                item["option_d"],
            ]
            item["correct_option"] = self._normalize_stored_correct_answer(
                item["options"],
                item["correct_option"],
            )
            questions.append(item)

        return questions

    def _normalize_question_record(self, item: dict) -> dict:
        for field_name in ("question_text", "option_a", "option_b", "option_c", "option_d", "correct_option", "explanation"):
            if field_name in item:
                item[field_name] = normalize_unicode_text(item.get(field_name))
        return item

    def add_exam(self, title: str, description: str | None = None):
        with database.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO exams (title, description, created_at) VALUES (?, ?, ?)",
                (title.strip(), description, timestamp()),
            )
            exam_id = cursor.lastrowid
        self.invalidate_cache()
        created_exam = self.get_exam(exam_id)
        logger.info("Exam insert success | exam_id=%s title=%s", exam_id, title.strip())
        return {
            "row_id": exam_id,
            "record": created_exam,
        }

    def delete_exam(self, exam_id: int):
        with database.connection() as conn:
            conn.execute("DELETE FROM exams WHERE exam_id = ?", (exam_id,))
        self.invalidate_cache()

    def add_set(self, exam_id: int, title: str, description: str | None = None):
        with database.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO exam_sets (exam_id, title, description, created_at) VALUES (?, ?, ?, ?)",
                (exam_id, title.strip(), description, timestamp()),
            )
            set_id = cursor.lastrowid
        self.invalidate_cache()
        created_set = self.get_set(set_id)
        logger.info("Set insert success | set_id=%s exam_id=%s title=%s", set_id, exam_id, title.strip())
        return {
            "row_id": set_id,
            "record": created_set,
        }

    def delete_set(self, set_id: int):
        with database.connection() as conn:
            conn.execute("DELETE FROM exam_sets WHERE set_id = ?", (set_id,))
        self.invalidate_cache()

    def set_set_premium_locked(self, set_id: int, is_locked: bool) -> dict | None:
        with database.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE exam_sets
                SET is_premium_locked = ?
                WHERE set_id = ?
                """,
                (1 if is_locked else 0, set_id),
            )

        if cursor.rowcount <= 0:
            return None

        self.invalidate_cache()
        return self.get_set(set_id)

    def add_question(
        self,
        exam_id: int,
        set_id: int,
        question_text: str,
        options: list[str],
        correct_option: str,
        image_path: str | None = None,
        time_limit: int | None = None,
    ):
        normalized_question_text = normalize_unicode_text(question_text.strip())
        normalized_options = [normalize_unicode_text(option.strip()) for option in options]
        normalized_correct_option = normalize_unicode_text(correct_option)

        with database.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO questions (
                    exam_id, set_id, question_text, option_a, option_b, option_c, option_d,
                    correct_option, explanation, image_path, time_limit, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    exam_id,
                    set_id,
                    normalized_question_text,
                    normalized_options[0],
                    normalized_options[1],
                    normalized_options[2],
                    normalized_options[3],
                    self._normalize_stored_correct_answer(normalized_options, normalized_correct_option),
                    image_path,
                    time_limit or DEFAULT_QUESTION_TIME,
                    timestamp(),
                ),
            )
            question_id = cursor.lastrowid
        self.invalidate_cache()
        created_question = self.get_question(question_id)
        logger.info(
            "Question insert success | question_id=%s exam_id=%s set_id=%s",
            question_id,
            exam_id,
            set_id,
        )
        return {
            "row_id": question_id,
            "record": created_question,
        }

    def get_exam(self, exam_id: int) -> dict | None:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM exams WHERE exam_id = ?",
                (exam_id,),
            ).fetchone()
        return dict(row) if row else None

    def delete_question(self, question_id: int):
        with database.connection() as conn:
            cursor = conn.execute("DELETE FROM questions WHERE question_id = ?", (question_id,))
            deleted = cursor.rowcount > 0
        self.invalidate_cache()
        return deleted

    def find_questions_by_text(self, search_text: str, limit: int = 10) -> list[dict]:
        pattern = f"%{search_text.strip()}%"
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    q.question_id,
                    q.question_text,
                    q.correct_option,
                    q.image_path,
                    q.time_limit,
                    s.title AS set_title,
                    e.title AS exam_title
                FROM questions q
                JOIN exam_sets s ON s.set_id = q.set_id
                JOIN exams e ON e.exam_id = q.exam_id
                WHERE q.question_text LIKE ?
                ORDER BY q.question_id DESC
                LIMIT ?
                """,
                (pattern, limit),
            ).fetchall()
        return [self._normalize_question_record(dict(row)) for row in rows]

    def get_question(self, question_id: int) -> dict | None:
        with database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM questions WHERE question_id = ?",
                (question_id,),
            ).fetchone()

        if not row:
            return None

        item = self._normalize_question_record(dict(row))
        item["options"] = [
            item["option_a"],
            item["option_b"],
            item["option_c"],
            item["option_d"],
        ]
        item["correct_option"] = self._normalize_stored_correct_answer(
            item["options"],
            item["correct_option"],
        )
        return item

    def migrate_correct_answers_to_text(self):
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT question_id, option_a, option_b, option_c, option_d, correct_option
                FROM questions
                """
            ).fetchall()

            for row in rows:
                options = [row["option_a"], row["option_b"], row["option_c"], row["option_d"]]
                normalized = self._normalize_stored_correct_answer(options, row["correct_option"])
                if normalized != row["correct_option"]:
                    conn.execute(
                        "UPDATE questions SET correct_option = ? WHERE question_id = ?",
                        (normalized, row["question_id"]),
                    )
        self.invalidate_cache()

    def import_legacy_data(self):
        data_file = DATA_DIR / "exams.json"
        if not data_file.exists():
            return

        with database.connection() as conn:
            count = conn.execute("SELECT COUNT(*) AS count FROM exams").fetchone()["count"]
            if count:
                return

        try:
            payload = json.loads(data_file.read_text(encoding="utf-8"))
        except Exception:
            return

        if not isinstance(payload, list):
            return

        for exam in payload:
            title = exam.get("name")
            if not title:
                continue
            self.add_exam(title)
            created_exam = next(item for item in self.get_exams() if item["title"] == title)
            for set_ in exam.get("sets", []):
                set_title = set_.get("name", "Set")
                self.add_set(created_exam["exam_id"], set_title)
                created_set = next(
                    item
                    for item in self.get_sets(created_exam["exam_id"])
                    if item["title"] == set_title
                )
                for question in set_.get("questions", []):
                    self.add_question(
                        created_exam["exam_id"],
                        created_set["set_id"],
                        question.get("question", "Untitled Question"),
                        self._normalize_options(question.get("options", [])),
                        self._resolve_correct_option(
                            question.get("options", []),
                            question.get("answer", ""),
                        ),
                        image_path=question.get("image"),
                        time_limit=question.get("time", DEFAULT_QUESTION_TIME),
                    )

    def _normalize_options(self, options: list[str]) -> list[str]:
        items = list(options)[:4]
        while len(items) < 4:
            items.append(f"Option {len(items) + 1}")
        return [str(item).strip() for item in items]

    def _resolve_correct_option(self, options: list[str], answer: str) -> str:
        normalized_options = self._normalize_options(options)
        normalized_answer = str(answer).strip()
        upper_answer = normalized_answer.upper()
        if upper_answer in {"A", "B", "C", "D"}:
            index = ("A", "B", "C", "D").index(upper_answer)
            return normalized_options[index]

        for option in normalized_options:
            if option.lower() == normalized_answer.lower():
                return option
        return normalized_options[0]

    def _normalize_stored_correct_answer(self, options: list[str], correct_option: str) -> str:
        normalized_options = self._normalize_options(options)
        normalized_correct = str(correct_option).strip()
        upper_correct = normalized_correct.upper()
        if upper_correct in {"A", "B", "C", "D"}:
            return normalized_options[("A", "B", "C", "D").index(upper_correct)]

        for option in normalized_options:
            if option.lower() == normalized_correct.lower():
                return option
        return normalized_options[0]


exam_service = ExamService()

