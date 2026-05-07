from db.database import database
from services.exam_service_db import exam_service, normalize_unicode_text
from services.payment_service_db import payment_service
from services.support_service_db import support_service
from services.user_service_db import user_service
from services.web_payment_service import web_payment_service


class WebAdminService:
    BULK_DELIMITER = "|"

    def dashboard_data(self) -> dict:
        users = user_service.list_users()
        payments = web_payment_service.list_payments()
        orders = web_payment_service.list_orders()
        exams = exam_service.get_exams()
        return {
            "users": users,
            "payments": payments,
            "orders": orders,
            "premium_prices": payment_service.list_premium_prices(),
            "support_tickets": self.list_support_tickets(),
            "admins": user_service.list_admins(),
            "non_admins": user_service.list_non_admins(),
            "exams": exams,
            "question_search_results": [],
            "dashboard_counts": {
                "total_users": len(users),
                "premium_users": sum(1 for user in users if user.get("is_premium")),
                "admin_users": sum(1 for user in users if user.get("user_role") in {"admin", "super_admin"} or user.get("is_admin")),
                "exam_count": len(exams),
                "question_count": sum(int(exam.get("question_count") or 0) for exam in exams),
                "payment_count": len(payments),
            },
        }

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

    def catalog_for_admin(self) -> list[dict]:
        catalog = []
        for exam in exam_service.get_exams():
            sets = []
            for set_item in exam_service.get_sets(exam["exam_id"]):
                sets.append(
                    {
                        **set_item,
                        "questions": self.list_questions_for_set(set_item["set_id"], limit=200),
                    }
                )
            catalog.append({**exam, "sets": sets})
        return catalog

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
        return [exam_service._normalize_question_record(dict(row)) for row in rows]

    def add_exam(self, title: str, description: str | None = None) -> dict:
        return exam_service.add_exam(title, description)

    def add_set(self, exam_id: int, title: str, description: str | None = None, is_premium_locked: bool = False) -> dict:
        result = exam_service.add_set(exam_id, title, description)
        exam_service.set_set_premium_locked(result["row_id"], is_premium_locked)
        return {"row_id": result["row_id"], "record": exam_service.get_set(result["row_id"])}

    def add_question(
        self,
        *,
        exam_id: int,
        set_id: int,
        question_text: str,
        options: list[str],
        correct_option: str,
        explanation: str | None = None,
        image_path: str | None = None,
        time_limit: int | None = None,
    ) -> dict:
        result = exam_service.add_question(
            exam_id=exam_id,
            set_id=set_id,
            question_text=question_text,
            options=options,
            correct_option=correct_option,
            image_path=image_path,
            time_limit=time_limit,
        )
        if explanation:
            with database.connection() as conn:
                conn.execute(
                    "UPDATE questions SET explanation = ? WHERE question_id = ?",
                    (normalize_unicode_text(explanation.strip()), result["row_id"]),
                )
            exam_service.invalidate_cache()
            result["record"] = exam_service.get_question(result["row_id"])
        return result

    def bulk_import_questions(self, *, exam_id: int, set_id: int, raw_text: str) -> list[dict]:
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
                    exam_id=exam_id,
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

    def change_user_role(self, target_user_id: int, role: str) -> tuple[dict | None, str]:
        normalized_role = (role or "").strip().lower()
        if normalized_role == "admin":
            return user_service.promote_to_admin(target_user_id)
        if normalized_role == "user":
            user = user_service.demote_admin(target_user_id)
            return user, "updated" if user else "not_found"
        raise ValueError("Unsupported role change. Only admin and user can be assigned from the web panel.")


web_admin_service = WebAdminService()
