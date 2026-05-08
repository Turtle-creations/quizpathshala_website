import copy
from decimal import Decimal

from flask import g, has_request_context

from db.database import database
from services.payment_service_db import SUBSCRIPTION_PLANS
from services.exam_service_db import exam_service
from services.support_service_db import support_service
from services.user_service_db import user_service


class WebAdminService:
    BULK_DELIMITER = "|"
    _DASHBOARD_CACHE_KEY = "_web_admin_dashboard_page_data"

    def dashboard_data(self) -> dict:
        data = self.dashboard_page_data()
        data.pop("catalog", None)
        data.pop("editing_question", None)
        return data

    def dashboard_page_data(self, *, search_text: str = "", edit_question_id: int | None = None) -> dict:
        normalized_search = (search_text or "").strip().casefold()
        cache_key = self._dashboard_cache_key(normalized_search, edit_question_id)
        cached_payload = self._get_cached_dashboard_page_data(cache_key)
        if cached_payload is not None:
            return copy.deepcopy(cached_payload)

        with database.connection() as conn:
            users = [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()]
            payments = [dict(row) for row in conn.execute(
                """
                SELECT * FROM payments
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (50,),
            ).fetchall()]
            orders = [dict(row) for row in conn.execute(
                """
                SELECT * FROM payment_orders
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (50,),
            ).fetchall()]
            support_tickets = [dict(row) for row in conn.execute(
                """
                SELECT * FROM support_messages
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (50,),
            ).fetchall()]
            settings_rows = conn.execute(
                """
                SELECT key, value
                FROM settings
                WHERE key IN (?, ?, ?)
                """,
                (
                    "premium_price:week_1",
                    "premium_price:month_1",
                    "premium_price:months_3",
                ),
            ).fetchall()
            exams = [dict(row) for row in conn.execute(
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
            ).fetchall()]
            set_rows = [dict(row) for row in conn.execute(
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
                GROUP BY s.set_id
                ORDER BY s.exam_id, s.title
                """
            ).fetchall()]
            question_rows = [dict(row) for row in conn.execute(
                """
                SELECT
                    q.*,
                    e.title AS exam_title,
                    s.title AS set_title
                FROM questions q
                JOIN exams e ON e.exam_id = q.exam_id
                JOIN exam_sets s ON s.set_id = q.set_id
                ORDER BY q.set_id, q.question_id DESC
                """
            ).fetchall()]

        admins = [user for user in users if user.get("user_role") in {"admin", "super_admin"} or user.get("is_admin")]
        non_admins = [user for user in users if user not in admins]
        premium_prices = self._premium_prices_from_settings(settings_rows)
        catalog, set_choices, question_search_results, editing_question = self._build_question_views(
            exams=exams,
            set_rows=set_rows,
            question_rows=question_rows,
            normalized_search=normalized_search,
            edit_question_id=edit_question_id,
        )

        payload = {
            "users": users,
            "payments": payments,
            "orders": orders,
            "premium_prices": premium_prices,
            "support_tickets": support_tickets,
            "admins": admins,
            "non_admins": non_admins,
            "exams": exams,
            "set_choices": set_choices,
            "catalog": catalog,
            "question_search_results": question_search_results,
            "editing_question": editing_question,
            "dashboard_counts": {
                "total_users": len(users),
                "premium_users": sum(1 for user in users if user.get("is_premium")),
                "admin_users": len(admins),
                "exam_count": len(exams),
                "question_count": sum(int(exam.get("question_count") or 0) for exam in exams),
                "payment_count": len(payments),
            },
        }
        self._set_cached_dashboard_page_data(cache_key, payload)
        return copy.deepcopy(payload)

    def list_support_tickets(self, limit: int = 50) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM support_messages
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_support_ticket(self, user: dict, message_text: str) -> int:
        return support_service.create_ticket(user, message_text)

    def list_set_choices(self) -> list[dict]:
        choices: list[dict] = []
        for exam in exam_service.get_exams():
            for set_item in exam_service.get_sets(int(exam["exam_id"])):
                choices.append(
                    {
                        "exam_id": int(exam["exam_id"]),
                        "exam_title": exam["title"],
                        "set_id": int(set_item["set_id"]),
                        "set_title": set_item["title"],
                        "label": f"{exam['title']} - {set_item['title']}",
                        "is_premium_locked": bool(int(set_item.get("is_premium_locked", 0))),
                        "question_count": int(set_item.get("question_count") or 0),
                    }
                )
        return choices

    def catalog_for_admin(self) -> list[dict]:
        exams = exam_service.get_exams()
        exam_map = {exam["exam_id"]: {**exam, "sets": []} for exam in exams}

        with database.connection() as conn:
            set_rows = conn.execute(
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
                GROUP BY s.set_id
                ORDER BY s.exam_id, s.title
                """
            ).fetchall()
            question_rows = conn.execute(
                """
                SELECT
                    q.*, e.title AS exam_title, s.title AS set_title
                FROM questions q
                JOIN exams e ON e.exam_id = q.exam_id
                JOIN exam_sets s ON s.set_id = q.set_id
                ORDER BY set_id, question_id DESC
                """
            ).fetchall()

        questions_by_set: dict[int, list[dict]] = {}
        for row in question_rows:
            set_id = int(row["set_id"])
            bucket = questions_by_set.setdefault(set_id, [])
            if len(bucket) >= 25:
                continue
            bucket.append(exam_service._serialize_question_row(dict(row)))

        for row in set_rows:
            set_item = dict(row)
            set_item["questions"] = questions_by_set.get(int(set_item["set_id"]), [])
            exam_entry = exam_map.get(int(set_item["exam_id"]))
            if exam_entry is not None:
                exam_entry["sets"].append(set_item)

        return list(exam_map.values())

    def list_questions_for_set(self, set_id: int, limit: int = 100) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM questions
                WHERE set_id = ?
                ORDER BY question_id DESC
                LIMIT ?
                """,
                (set_id, limit),
            ).fetchall()
        return [exam_service._serialize_question_row(dict(row)) for row in rows]

    def add_exam(self, title: str, description: str | None = None) -> dict:
        return exam_service.add_exam(title, description)

    def add_set(self, exam_id: int, title: str, description: str | None = None, is_premium_locked: bool = False) -> dict:
        result = exam_service.add_set(exam_id, title, description)
        exam_service.set_set_premium_locked(result["row_id"], is_premium_locked)
        return {"row_id": result["row_id"], "record": exam_service.get_set(result["row_id"])}

    def add_question(
        self,
        *,
        set_id: int,
        question_text: str,
        options: list[str],
        correct_option: str,
        explanation: str | None = None,
        image_path: str | None = None,
        time_limit: int | None = None,
    ) -> dict:
        return exam_service.save_question(
            set_id=set_id,
            question_text=question_text,
            options=options,
            correct_option=correct_option,
            explanation=explanation,
            image_path=image_path,
            time_limit=time_limit,
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
    ) -> dict:
        return exam_service.update_question(
            question_id=question_id,
            set_id=set_id,
            question_text=question_text,
            options=options,
            correct_option=correct_option,
            explanation=explanation,
            image_path=image_path,
            time_limit=time_limit,
        )

    def bulk_import_questions(self, *, set_id: int, raw_text: str) -> list[dict]:
        created = []
        for line_number, line in enumerate(raw_text.splitlines(), start=1):
            cleaned = line.strip()
            if not cleaned:
                continue
            parts = [part.strip() for part in cleaned.split(self.BULK_DELIMITER)]
            if len(parts) < 6:
                raise ValueError(
                    f"Line {line_number}: expected at least 6 pipe-separated columns: question|A|B|C|D|answer|explanation(optional)|time(optional)|image(optional)"
                )

            question_text, option_a, option_b, option_c, option_d, correct_option, *rest = parts
            explanation = rest[0] if len(rest) >= 1 and rest[0] else None
            time_limit = int(rest[1]) if len(rest) >= 2 and rest[1] else None
            image_path = rest[2] if len(rest) >= 3 and rest[2] else None
            created.append(
                self.add_question(
                    set_id=set_id,
                    question_text=question_text,
                    options=[option_a, option_b, option_c, option_d],
                    correct_option=correct_option,
                    explanation=explanation,
                    image_path=image_path,
                    time_limit=time_limit,
                )
            )
        return created

    def delete_exam(self, exam_id: int) -> None:
        exam_service.delete_exam(exam_id)

    def delete_set(self, set_id: int) -> None:
        exam_service.delete_set(set_id)

    def delete_question(self, question_id: int) -> bool:
        return exam_service.delete_question(question_id)

    def set_set_lock(self, set_id: int, is_locked: bool) -> dict | None:
        return exam_service.set_set_premium_locked(set_id, is_locked)

    def search_questions(self, search_text: str, limit: int = 25) -> list[dict]:
        if not (search_text or "").strip():
            return []
        return exam_service.find_questions_by_text(search_text, limit=limit)

    def get_question(self, question_id: int) -> dict | None:
        return exam_service.get_question(question_id)

    def change_user_role(self, target_user_id: int, role: str) -> tuple[dict | None, str]:
        normalized_role = (role or "").strip().lower()
        if normalized_role == "admin":
            return user_service.promote_to_admin(target_user_id)
        if normalized_role == "user":
            user = user_service.demote_admin(target_user_id)
            return user, "updated" if user else "not_found"
        raise ValueError("Unsupported role change. Only admin and user can be assigned from the web panel.")

    def _premium_prices_from_settings(self, settings_rows: list[dict]) -> list[dict]:
        stored_values = {str(row["key"]): str(row["value"]) for row in settings_rows}
        items = []
        for display_key, internal_key in (("week_1", "week_1"), ("month_1", "month_1"), ("month_3", "months_3")):
            plan = dict(SUBSCRIPTION_PLANS[internal_key])
            setting_key = f"premium_price:{internal_key}"
            if setting_key in stored_values:
                plan["amount"] = int(stored_values[setting_key])
            items.append(
                {
                    "key": display_key,
                    "plan_type": internal_key,
                    "name": plan["name"],
                    "amount_paise": int(plan["amount"]),
                    "amount_rupees": Decimal(int(plan["amount"])) / Decimal("100"),
                }
            )
        return items

    def _build_question_views(
        self,
        *,
        exams: list[dict],
        set_rows: list[dict],
        question_rows: list[dict],
        normalized_search: str,
        edit_question_id: int | None,
    ) -> tuple[list[dict], list[dict], list[dict], dict | None]:
        exam_map = {int(exam["exam_id"]): {**exam, "sets": []} for exam in exams}
        set_choices: list[dict] = []
        questions_by_set: dict[int, list[dict]] = {}
        question_by_id: dict[int, dict] = {}
        question_search_results: list[dict] = []

        for row in question_rows:
            serialized = exam_service._serialize_question_row(row)
            question_id = int(serialized["question_id"])
            set_id = int(serialized["set_id"])
            question_by_id[question_id] = serialized

            bucket = questions_by_set.setdefault(set_id, [])
            if len(bucket) < 25:
                bucket.append(serialized)

            if normalized_search and normalized_search in str(serialized.get("question_text") or "").casefold():
                question_search_results.append(
                    {
                        "question_id": question_id,
                        "exam_id": int(serialized["exam_id"]),
                        "set_id": set_id,
                        "question_text": serialized.get("question_text"),
                        "correct_option": serialized.get("correct_option"),
                        "explanation": serialized.get("explanation"),
                        "image_path": serialized.get("image_path"),
                        "time_limit": serialized.get("time_limit"),
                        "set_title": serialized.get("set_title"),
                        "exam_title": serialized.get("exam_title"),
                    }
                )

        for row in set_rows:
            set_id = int(row["set_id"])
            exam_id = int(row["exam_id"])
            set_item = dict(row)
            set_item["questions"] = questions_by_set.get(set_id, [])
            set_choices.append(
                {
                    "exam_id": exam_id,
                    "exam_title": exam_map.get(exam_id, {}).get("title", ""),
                    "set_id": set_id,
                    "set_title": set_item["title"],
                    "label": f"{exam_map.get(exam_id, {}).get('title', '')} - {set_item['title']}",
                    "is_premium_locked": bool(int(set_item.get("is_premium_locked", 0))),
                    "question_count": int(set_item.get("question_count") or 0),
                }
            )
            exam_entry = exam_map.get(exam_id)
            if exam_entry is not None:
                exam_entry["sets"].append(set_item)

        return (
            list(exam_map.values()),
            set_choices,
            question_search_results[:25],
            question_by_id.get(int(edit_question_id)) if edit_question_id else None,
        )

    def _dashboard_cache_key(self, normalized_search: str, edit_question_id: int | None) -> tuple[str, int | None]:
        return (normalized_search, int(edit_question_id) if edit_question_id else None)

    def _get_cached_dashboard_page_data(self, cache_key):
        if not has_request_context():
            return None
        cache = getattr(g, self._DASHBOARD_CACHE_KEY, None)
        if not isinstance(cache, dict):
            return None
        return cache.get(cache_key)

    def _set_cached_dashboard_page_data(self, cache_key, payload: dict) -> None:
        if not has_request_context():
            return
        cache = getattr(g, self._DASHBOARD_CACHE_KEY, None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(g, self._DASHBOARD_CACHE_KEY, cache)
        cache[cache_key] = copy.deepcopy(payload)


web_admin_service = WebAdminService()
