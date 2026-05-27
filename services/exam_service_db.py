import json
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache

from config import DATA_DIR, DEFAULT_QUESTION_TIME
from db.database import database
from utils.logging_utils import get_logger


def timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


logger = get_logger(__name__)


class SimilarQuestionError(ValueError):
    def __init__(self, message: str, *, duplicate_details: dict):
        super().__init__(message)
        self.duplicate_details = duplicate_details


def normalize_unicode_text(value: str | None) -> str | None:
    if value is None:
        return None

    text = str(value)
    if not text:
        return text

    suspicious_markers = ("à¤", "à¥", "Ã", "Â", "â€", "ðŸ")
    if not any(marker in text for marker in suspicious_markers):
        return text

    try:
        repaired = text.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

    return repaired if repaired else text


class ExamService:
    def exam_storage_ready(self) -> bool:
        return database.tables_exist({"categories", "sub_exams", "exams", "exam_sets", "questions"})

    def invalidate_cache(self):
        self.get_categories.cache_clear()
        self.get_sub_exams.cache_clear()
        self.list_exam_hierarchy.cache_clear()
        self.get_exams.cache_clear()
        self.get_sets.cache_clear()
        self.get_set.cache_clear()
        self.get_questions.cache_clear()

    @lru_cache(maxsize=1)
    def get_categories(self) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.category_id,
                    c.name,
                    COUNT(DISTINCT se.sub_exam_id) AS sub_exam_count
                FROM categories c
                LEFT JOIN sub_exams se ON se.category_id = c.category_id
                GROUP BY c.category_id, c.name
                ORDER BY c.name, c.category_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @lru_cache(maxsize=1)
    def get_sub_exams(self) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    se.sub_exam_id,
                    se.name,
                    se.category_id,
                    c.name AS category_name,
                    e.exam_id,
                    e.description,
                    COUNT(DISTINCT s.set_id) AS set_count,
                    COUNT(q.question_id) AS question_count
                FROM sub_exams se
                JOIN categories c ON c.category_id = se.category_id
                LEFT JOIN exams e ON e.sub_exam_id = se.sub_exam_id
                LEFT JOIN exam_sets s ON s.exam_id = e.exam_id
                LEFT JOIN questions q ON q.set_id = s.set_id
                GROUP BY
                    se.sub_exam_id,
                    se.name,
                    se.category_id,
                    c.name,
                    e.exam_id,
                    e.description
                ORDER BY c.name, se.name, se.sub_exam_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    @lru_cache(maxsize=1)
    def list_exam_hierarchy(self) -> list[dict]:
        categories = []
        sub_exams = self.get_sub_exams()
        grouped: dict[int, dict] = {}
        for category in self.get_categories():
            bucket = {
                "category_id": int(category["category_id"]),
                "name": category["name"],
                "sub_exams": [],
            }
            grouped[bucket["category_id"]] = bucket
            categories.append(bucket)
        for sub_exam in sub_exams:
            bucket = grouped.get(int(sub_exam["category_id"]))
            if not bucket or not sub_exam.get("exam_id"):
                continue
            bucket["sub_exams"].append(dict(sub_exam))
        return categories

    @lru_cache(maxsize=1)
    def get_exams(self) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.exam_id,
                    e.title,
                    e.sub_exam_id,
                    e.description,
                    se.name AS sub_exam_name,
                    se.category_id,
                    c.name AS category_name,
                    COUNT(DISTINCT s.set_id) AS set_count,
                    COUNT(q.question_id) AS question_count
                FROM exams e
                LEFT JOIN sub_exams se ON se.sub_exam_id = e.sub_exam_id
                LEFT JOIN categories c ON c.category_id = se.category_id
                LEFT JOIN exam_sets s ON s.exam_id = e.exam_id
                LEFT JOIN questions q ON q.set_id = s.set_id
                GROUP BY
                    e.exam_id,
                    e.title,
                    e.sub_exam_id,
                    e.description,
                    se.name,
                    se.category_id,
                    c.name
                ORDER BY c.name, se.name, e.title, e.exam_id
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
                    s.position,
                    COUNT(q.question_id) AS question_count
                FROM exam_sets s
                LEFT JOIN questions q ON q.set_id = s.set_id
                WHERE s.exam_id = ?
                GROUP BY s.set_id
                ORDER BY COALESCE(s.position, s.set_id), s.set_id
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
                    s.position,
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
            item = self._serialize_question_row(dict(row))
            questions.append(item)

        return questions

    def _normalize_question_record(self, item: dict) -> dict:
        for field_name in ("question_text", "option_a", "option_b", "option_c", "option_d", "correct_option", "explanation"):
            if field_name in item:
                item[field_name] = normalize_unicode_text(item.get(field_name))
        return item

    def _serialize_question_row(self, row: dict) -> dict:
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

    def _clean_question_text(self, value: str | None) -> str:
        return " ".join(str(value or "").strip().split())

    def _build_question_match_key(self, value: str | None) -> str:
        return self._clean_question_text(value).casefold()

    def _coerce_time_limit(self, time_limit: int | None) -> int:
        parsed = int(time_limit or DEFAULT_QUESTION_TIME)
        return parsed if parsed > 0 else DEFAULT_QUESTION_TIME

    def _default_category_id(self, conn) -> int:
        row = conn.execute(
            "SELECT category_id FROM categories WHERE name = ?",
            ("Uncategorized",),
        ).fetchone()
        if row:
            return int(row["category_id"])
        return int(conn.execute("INSERT INTO categories (name) VALUES (?)", ("Uncategorized",)).lastrowid)

    def _next_set_position(self, conn, exam_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS max_position FROM exam_sets WHERE exam_id = ?",
            (exam_id,),
        ).fetchone()
        return int(row["max_position"] or 0) + 1

    def add_category(self, name: str) -> dict:
        cleaned_name = " ".join(str(name or "").split())
        if not cleaned_name:
            raise ValueError("Category name is required.")
        with database.connection() as conn:
            category_id = int(
                conn.execute(
                    "INSERT INTO categories (name) VALUES (?)",
                    (cleaned_name,),
                ).lastrowid
            )
        self.invalidate_cache()
        return self.get_category(category_id) or {}

    def update_category(self, category_id: int, name: str) -> dict | None:
        cleaned_name = " ".join(str(name or "").split())
        if not cleaned_name:
            raise ValueError("Category name is required.")
        with database.connection() as conn:
            cursor = conn.execute(
                "UPDATE categories SET name = ? WHERE category_id = ?",
                (cleaned_name, category_id),
            )
        if cursor.rowcount <= 0:
            return None
        self.invalidate_cache()
        return self.get_category(category_id)

    def delete_category(self, category_id: int):
        with database.connection() as conn:
            sub_exam_rows = conn.execute(
                "SELECT sub_exam_id FROM sub_exams WHERE category_id = ?",
                (category_id,),
            ).fetchall()
            for row in sub_exam_rows:
                conn.execute("DELETE FROM exams WHERE sub_exam_id = ?", (row["sub_exam_id"],))
            conn.execute("DELETE FROM sub_exams WHERE category_id = ?", (category_id,))
            conn.execute("DELETE FROM categories WHERE category_id = ?", (category_id,))
        self.invalidate_cache()

    def add_sub_exam(self, name: str, category_id: int, description: str | None = None) -> dict:
        cleaned_name = " ".join(str(name or "").split())
        if not cleaned_name:
            raise ValueError("Sub-exam name is required.")
        with database.connection() as conn:
            sub_exam_id = int(
                conn.execute(
                    "INSERT INTO sub_exams (name, category_id) VALUES (?, ?)",
                    (cleaned_name, category_id),
                ).lastrowid
            )
            exam_id = int(
                conn.execute(
                    "INSERT INTO exams (title, sub_exam_id, description, created_at) VALUES (?, ?, ?, ?)",
                    (cleaned_name, sub_exam_id, description, timestamp()),
                ).lastrowid
            )
        self.invalidate_cache()
        created_exam = self.get_exam(exam_id)
        logger.info("Sub-exam insert success | sub_exam_id=%s exam_id=%s name=%s", sub_exam_id, exam_id, cleaned_name)
        return {
            "row_id": exam_id,
            "sub_exam_id": sub_exam_id,
            "record": created_exam,
        }

    def update_sub_exam(self, sub_exam_id: int, name: str, category_id: int) -> dict | None:
        cleaned_name = " ".join(str(name or "").split())
        if not cleaned_name:
            raise ValueError("Sub-exam name is required.")
        with database.connection() as conn:
            sub_exam_cursor = conn.execute(
                "UPDATE sub_exams SET name = ?, category_id = ? WHERE sub_exam_id = ?",
                (cleaned_name, category_id, sub_exam_id),
            )
            conn.execute(
                "UPDATE exams SET title = ? WHERE sub_exam_id = ?",
                (cleaned_name, sub_exam_id),
            )
        if sub_exam_cursor.rowcount <= 0:
            return None
        self.invalidate_cache()
        return self.get_sub_exam(sub_exam_id)

    def delete_sub_exam(self, sub_exam_id: int):
        with database.connection() as conn:
            conn.execute("DELETE FROM exams WHERE sub_exam_id = ?", (sub_exam_id,))
            conn.execute("DELETE FROM sub_exams WHERE sub_exam_id = ?", (sub_exam_id,))
        self.invalidate_cache()

    def add_exam(self, title: str, description: str | None = None):
        with database.connection() as conn:
            category_id = self._default_category_id(conn)
        return self.add_sub_exam(title, category_id, description)

    def delete_exam(self, exam_id: int):
        with database.connection() as conn:
            row = conn.execute(
                "SELECT sub_exam_id FROM exams WHERE exam_id = ?",
                (exam_id,),
            ).fetchone()
            conn.execute("DELETE FROM exams WHERE exam_id = ?", (exam_id,))
            if row and row["sub_exam_id"]:
                conn.execute("DELETE FROM sub_exams WHERE sub_exam_id = ?", (row["sub_exam_id"],))
        self.invalidate_cache()

    def add_set(self, exam_id: int, title: str, description: str | None = None):
        with database.connection() as conn:
            position = self._next_set_position(conn, exam_id)
            cursor = conn.execute(
                "INSERT INTO exam_sets (exam_id, title, description, position, created_at) VALUES (?, ?, ?, ?, ?)",
                (exam_id, title.strip(), description, position, timestamp()),
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

    def update_set_position(self, set_id: int, position: int) -> dict | None:
        normalized_position = max(int(position), 1)
        with database.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE exam_sets
                SET position = ?
                WHERE set_id = ?
                """,
                (normalized_position, set_id),
            )

        if cursor.rowcount <= 0:
            return None

        self.invalidate_cache()
        return self.get_set(set_id)

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

    def _question_similarity_ratio(self, first: str | None, second: str | None) -> float:
        first_key = self._build_question_match_key(first)
        second_key = self._build_question_match_key(second)
        if not first_key or not second_key:
            return 0.0
        if first_key == second_key:
            return 1.0
        return SequenceMatcher(None, first_key, second_key).ratio()

    def _find_similar_question(
        self,
        conn,
        *,
        set_id: int,
        question_text: str,
        exclude_question_id: int | None = None,
        threshold: float = 0.98,
    ) -> dict | None:
        rows = conn.execute(
            """
            SELECT
                question_id,
                question_text,
                option_a,
                option_b,
                option_c,
                option_d,
                correct_option,
                explanation,
                image_path,
                time_limit
            FROM questions
            WHERE set_id = ?
            ORDER BY question_id
            """,
            (set_id,),
        ).fetchall()

        best_match = None
        for row in rows:
            row_question_id = int(row["question_id"])
            if exclude_question_id is not None and row_question_id == exclude_question_id:
                continue
            similarity_ratio = self._question_similarity_ratio(question_text, row["question_text"])
            if similarity_ratio < threshold:
                continue
            candidate = dict(row)
            candidate["similarity_ratio"] = similarity_ratio
            if best_match is None or similarity_ratio > float(best_match["similarity_ratio"]):
                best_match = candidate
        return self._serialize_question_row(best_match) if best_match else None

    def save_question(
        self,
        *,
        set_id: int,
        question_text: str,
        options: list[str],
        correct_option: str,
        explanation: str | None = None,
        image_path: str | None = None,
        time_limit: int | None = None,
        question_id: int | None = None,
        allow_similar_duplicate: bool = False,
    ) -> dict:
        set_item = self.get_set(int(set_id))
        if not set_item:
            raise ValueError("Please select a valid set.")

        cleaned_question_text = self._clean_question_text(question_text)
        cleaned_options = self._normalize_options(options)
        cleaned_correct_option = self._normalize_stored_correct_answer(cleaned_options, str(correct_option or "").strip())
        cleaned_explanation = str(explanation).strip() if explanation else None
        cleaned_image_path = str(image_path).strip() or None if image_path is not None else None
        cleaned_time_limit = self._coerce_time_limit(time_limit)

        if not cleaned_question_text:
            raise ValueError("Question text is required.")

        with database.connection() as conn:
            similar_question = self._find_similar_question(
                conn,
                set_id=int(set_id),
                question_text=cleaned_question_text,
                exclude_question_id=int(question_id) if question_id else None,
            )
            if similar_question and not allow_similar_duplicate:
                raise SimilarQuestionError(
                    "Similar question already exists in this set",
                    duplicate_details={
                        "message": "Similar question already exists in this set",
                        "set_id": int(set_id),
                        "set_title": set_item.get("title"),
                        "question_id": int(question_id) if question_id else None,
                        "matched_question": similar_question,
                    },
                )

            duplicate_action = "updated" if question_id else "created"
            target_question_id = int(question_id) if question_id else None

            if target_question_id is not None:
                cursor = conn.execute(
                    """
                    UPDATE questions
                    SET exam_id = ?,
                        set_id = ?,
                        question_text = ?,
                        option_a = ?,
                        option_b = ?,
                        option_c = ?,
                        option_d = ?,
                        correct_option = ?,
                        explanation = ?,
                        image_path = ?,
                        time_limit = ?
                    WHERE question_id = ?
                    """,
                    (
                        int(set_item["exam_id"]),
                        int(set_id),
                        cleaned_question_text,
                        cleaned_options[0],
                        cleaned_options[1],
                        cleaned_options[2],
                        cleaned_options[3],
                        cleaned_correct_option,
                        cleaned_explanation,
                        cleaned_image_path,
                        cleaned_time_limit,
                        target_question_id,
                    ),
                )
                if cursor.rowcount <= 0:
                    raise ValueError("Question not found.")
                saved_question_id = target_question_id
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO questions (
                        exam_id, set_id, question_text, option_a, option_b, option_c, option_d,
                        correct_option, explanation, image_path, time_limit, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(set_item["exam_id"]),
                        int(set_id),
                        cleaned_question_text,
                        cleaned_options[0],
                        cleaned_options[1],
                        cleaned_options[2],
                        cleaned_options[3],
                        cleaned_correct_option,
                        cleaned_explanation,
                        cleaned_image_path,
                        cleaned_time_limit,
                        timestamp(),
                    ),
                )
                saved_question_id = int(cursor.lastrowid)

        self.invalidate_cache()
        saved_question = self.get_question(saved_question_id)
        logger.info(
            "Question save success | action=%s question_id=%s set_id=%s exam_id=%s",
            duplicate_action,
            saved_question_id,
            set_id,
            set_item["exam_id"],
        )
        return {
            "row_id": saved_question_id,
            "record": saved_question,
            "operation": duplicate_action,
        }

    def add_question(
        self,
        exam_id: int,
        set_id: int,
        question_text: str,
        options: list[str],
        correct_option: str,
        image_path: str | None = None,
        time_limit: int | None = None,
        explanation: str | None = None,
    ):
        return self.save_question(
            set_id=set_id,
            question_text=question_text,
            options=options,
            correct_option=correct_option,
            explanation=explanation,
            image_path=image_path,
            time_limit=time_limit,
            allow_similar_duplicate=False,
        )

    def update_question(
        self,
        *,
        question_id: int,
        set_id: int,
        question_text: str,
        options: list[str],
        correct_option: str,
        explanation: str | None = None,
        image_path: str | None = None,
        time_limit: int | None = None,
        allow_similar_duplicate: bool = False,
    ) -> dict:
        return self.save_question(
            question_id=question_id,
            set_id=set_id,
            question_text=question_text,
            options=options,
            correct_option=correct_option,
            explanation=explanation,
            image_path=image_path,
            time_limit=time_limit,
            allow_similar_duplicate=allow_similar_duplicate,
        )

    def get_category(self, category_id: int) -> dict | None:
        with database.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    c.category_id,
                    c.name,
                    COUNT(DISTINCT se.sub_exam_id) AS sub_exam_count
                FROM categories c
                LEFT JOIN sub_exams se ON se.category_id = c.category_id
                WHERE c.category_id = ?
                GROUP BY c.category_id, c.name
                """,
                (category_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_sub_exam(self, sub_exam_id: int) -> dict | None:
        with database.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    se.sub_exam_id,
                    se.name,
                    se.category_id,
                    c.name AS category_name,
                    e.exam_id,
                    e.description
                FROM sub_exams se
                JOIN categories c ON c.category_id = se.category_id
                LEFT JOIN exams e ON e.sub_exam_id = se.sub_exam_id
                WHERE se.sub_exam_id = ?
                """,
                (sub_exam_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_exam(self, exam_id: int) -> dict | None:
        with database.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    e.*,
                    se.name AS sub_exam_name,
                    se.category_id,
                    c.name AS category_name
                FROM exams e
                LEFT JOIN sub_exams se ON se.sub_exam_id = e.sub_exam_id
                LEFT JOIN categories c ON c.category_id = se.category_id
                WHERE e.exam_id = ?
                """,
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
                    q.exam_id,
                    q.set_id,
                    q.question_text,
                    q.correct_option,
                    q.explanation,
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
                """
                SELECT
                    q.*,
                    s.title AS set_title,
                    e.title AS exam_title
                FROM questions q
                JOIN exam_sets s ON s.set_id = q.set_id
                JOIN exams e ON e.exam_id = q.exam_id
                WHERE q.question_id = ?
                """,
                (question_id,),
            ).fetchone()

        if not row:
            return None

        return self._serialize_question_row(dict(row))

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
        items = [str(item or "").strip() for item in list(options)[:4]]
        while len(items) < 4:
            items.append(f"Option {len(items) + 1}")
        return items

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
