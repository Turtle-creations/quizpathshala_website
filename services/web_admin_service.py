import copy
import math
import re
from decimal import Decimal, InvalidOperation

from flask import g, has_request_context

from db.database import database
from services.exam_service_db import exam_service
from services.payment_service_db import SUBSCRIPTION_PLANS
from services.support_service_db import support_service
from services.user_service_db import now_iso, user_service


class WebAdminService:
    BULK_DELIMITER = "|"
    _DASHBOARD_CACHE_KEY = "_web_admin_dashboard_page_data"
    _PLAN_PERIOD_ORDER = {"monthly": 1, "annual": 2}

    def dashboard_data(self) -> dict:
        data = self.dashboard_page_data()
        data.pop("editing_question", None)
        data.pop("editing_plan", None)
        return data

    def dashboard_page_data(
        self,
        *,
        search_text: str = "",
        edit_question_id: int | None = None,
        edit_plan_id: int | None = None,
        users_page: int = 1,
        payments_page: int = 1,
        orders_page: int = 1,
        support_page: int = 1,
        reports_page: int = 1,
        user_logs_page: int = 1,
        users_from: str | None = None,
        users_to: str | None = None,
        logs_from: str | None = None,
        logs_to: str | None = None,
    ) -> dict:
        normalized_search = (search_text or "").strip().casefold()
        cache_key = self._dashboard_cache_key(
            normalized_search,
            edit_question_id,
            edit_plan_id,
            users_page,
            payments_page,
            orders_page,
            support_page,
            reports_page,
            user_logs_page,
            users_from,
            users_to,
            logs_from,
            logs_to,
        )
        cached_payload = self._get_cached_dashboard_page_data(cache_key)
        if cached_payload is not None:
            return copy.deepcopy(cached_payload)

        with database.connection() as conn:
            users, users_pagination = self._list_users_page(
                conn,
                page=users_page,
                date_from=users_from,
                date_to=users_to,
            )
            payments, payments_pagination = self._list_payments_page(
                conn,
                page=payments_page,
                date_from=logs_from,
                date_to=logs_to,
            )
            orders, orders_pagination = self._list_orders_page(
                conn,
                page=orders_page,
                date_from=logs_from,
                date_to=logs_to,
            )
            support_tickets, support_pagination = self._list_support_page(
                conn,
                page=support_page,
                date_from=logs_from,
                date_to=logs_to,
            )
            question_reports, reports_pagination = self._list_reports_page(
                conn,
                page=reports_page,
                date_from=logs_from,
                date_to=logs_to,
            )
            user_logs, user_logs_pagination = self._list_user_logs_page(
                conn,
                page=user_logs_page,
                date_from=logs_from,
                date_to=logs_to,
            )
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
            total_users = int(conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"] or 0)
            premium_users = int(
                conn.execute("SELECT COUNT(*) AS count FROM users WHERE is_premium = 1").fetchone()["count"] or 0
            )
            admin_users = int(
                conn.execute(
                    "SELECT COUNT(*) AS count FROM users WHERE is_admin = 1 OR user_role IN ('admin', 'super_admin')"
                ).fetchone()["count"] or 0
            )

        exams = self.list_exams_overview()
        premium_prices = self._premium_prices_from_settings(settings_rows)
        plans = self.list_admin_plans()
        set_choices = self.list_set_choices()
        question_search_results = self.search_questions(search_text, limit=25) if normalized_search else []
        editing_question = self.get_question(edit_question_id) if edit_question_id else None
        editing_plan = self.get_admin_plan(edit_plan_id) if edit_plan_id else None

        payload = {
            "users": users,
            "users_pagination": users_pagination,
            "payments": payments,
            "payments_pagination": payments_pagination,
            "orders": orders,
            "orders_pagination": orders_pagination,
            "support_tickets": support_tickets,
            "support_pagination": support_pagination,
            "question_reports": question_reports,
            "reports_pagination": reports_pagination,
            "user_logs": user_logs,
            "user_logs_pagination": user_logs_pagination,
            "premium_prices": premium_prices,
            "plans": plans,
            "editing_plan": editing_plan,
            "exams": exams,
            "set_choices": set_choices,
            "question_search_results": question_search_results,
            "editing_question": editing_question,
            "dashboard_counts": {
                "total_users": total_users,
                "premium_users": premium_users,
                "admin_users": admin_users,
                "exam_count": len(exams),
                "question_count": sum(int(exam.get("question_count") or 0) for exam in exams),
                "payment_count": payments_pagination["total_items"],
            },
            "filters": {
                "users_from": (users_from or "").strip(),
                "users_to": (users_to or "").strip(),
                "logs_from": (logs_from or "").strip(),
                "logs_to": (logs_to or "").strip(),
            },
        }
        self._set_cached_dashboard_page_data(cache_key, payload)
        return copy.deepcopy(payload)

    def list_support_tickets(self, limit: int = 50) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM support_messages
                ORDER BY created_at DESC, support_id DESC
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

    def list_exams_overview(self) -> list[dict]:
        return exam_service.get_exams()

    def get_exam_overview(self, exam_id: int) -> dict | None:
        return exam_service.get_exam(exam_id)

    def list_sets_for_exam(self, exam_id: int, page: int = 1, per_page: int = 10) -> tuple[dict | None, list[dict], dict]:
        exam = exam_service.get_exam(exam_id)
        if not exam:
            return None, [], self._empty_pagination(per_page)
        all_sets = exam_service.get_sets(exam_id)
        paginated_sets, pagination = self._paginate_list(all_sets, page=page, per_page=per_page)
        return exam, paginated_sets, pagination

    def get_set_overview(self, set_id: int) -> dict | None:
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
                    COUNT(q.question_id) AS question_count,
                    e.title AS exam_title,
                    e.description AS exam_description
                FROM exam_sets s
                JOIN exams e ON e.exam_id = s.exam_id
                LEFT JOIN questions q ON q.set_id = s.set_id
                WHERE s.set_id = ?
                GROUP BY
                    s.set_id,
                    s.exam_id,
                    s.title,
                    s.description,
                    s.is_premium_locked,
                    s.position,
                    e.title,
                    e.description
                """,
                (set_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_questions_for_set(self, set_id: int, limit: int | None = None) -> list[dict]:
        query = """
            SELECT
                q.*, e.title AS exam_title, s.title AS set_title
            FROM questions q
            JOIN exams e ON e.exam_id = q.exam_id
            JOIN exam_sets s ON s.set_id = q.set_id
            WHERE q.set_id = ?
            ORDER BY q.question_id DESC
        """
        params: list[int] = [set_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with database.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [exam_service._serialize_question_row(dict(row)) for row in rows]

    def add_exam(self, title: str, description: str | None = None) -> dict:
        return exam_service.add_exam(title, description)

    def add_set(self, exam_id: int, title: str, description: str | None = None, is_premium_locked: bool = False) -> dict:
        result = exam_service.add_set(exam_id, title, description)
        exam_service.set_set_premium_locked(result["row_id"], is_premium_locked)
        return {"row_id": result["row_id"], "record": exam_service.get_set(result["row_id"])}

    def update_set_position(self, set_id: int, position: int) -> dict | None:
        return exam_service.update_set_position(set_id, position)

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

    def get_user_detail(self, user_id: int) -> dict | None:
        user = user_service.get_user(user_id)
        if not user:
            return None

        with database.connection() as conn:
            attempts_summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_attempts,
                    COALESCE(SUM(correct_count), 0) AS total_correct,
                    COALESCE(SUM(wrong_count), 0) AS total_wrong,
                    COALESCE(SUM(skipped_count), 0) AS total_skipped,
                    MAX(created_at) AS last_attempt_at
                FROM quiz_attempts
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            payments_summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS payment_count,
                    COALESCE(SUM(amount), 0) AS total_paid,
                    MAX(timestamp) AS last_payment_at
                FROM payments
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            orders_summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS order_count,
                    MAX(created_at) AS last_order_at
                FROM payment_orders
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            reports_summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS report_count,
                    MAX(created_at) AS last_report_at
                FROM question_reports
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            tickets_summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS ticket_count,
                    MAX(created_at) AS last_ticket_at
                FROM support_messages
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            recent_attempts = conn.execute(
                """
                SELECT
                    qa.attempt_id,
                    qa.created_at,
                    qa.requested_count,
                    qa.correct_count,
                    qa.wrong_count,
                    qa.skipped_count,
                    qa.ended_reason,
                    s.title AS set_title,
                    e.title AS exam_title
                FROM quiz_attempts qa
                LEFT JOIN exam_sets s ON s.set_id = qa.set_id
                LEFT JOIN exams e ON e.exam_id = s.exam_id
                WHERE qa.user_id = ?
                ORDER BY qa.created_at DESC, qa.attempt_id DESC
                LIMIT 10
                """,
                (user_id,),
            ).fetchall()

        return {
            "user": user,
            "activity_summary": {
                "total_attempts": int(attempts_summary["total_attempts"] or 0),
                "total_correct": int(attempts_summary["total_correct"] or 0),
                "total_wrong": int(attempts_summary["total_wrong"] or 0),
                "total_skipped": int(attempts_summary["total_skipped"] or 0),
                "last_attempt_at": attempts_summary["last_attempt_at"],
                "payment_count": int(payments_summary["payment_count"] or 0),
                "total_paid_paise": int(payments_summary["total_paid"] or 0),
                "last_payment_at": payments_summary["last_payment_at"],
                "order_count": int(orders_summary["order_count"] or 0),
                "last_order_at": orders_summary["last_order_at"],
                "report_count": int(reports_summary["report_count"] or 0),
                "last_report_at": reports_summary["last_report_at"],
                "ticket_count": int(tickets_summary["ticket_count"] or 0),
                "last_ticket_at": tickets_summary["last_ticket_at"],
            },
            "recent_attempts": [dict(row) for row in recent_attempts],
        }

    def change_user_role(self, target_user_id: int, role: str) -> tuple[dict | None, str]:
        normalized_role = (role or "").strip().lower()
        if normalized_role == "admin":
            return user_service.promote_to_admin(target_user_id)
        if normalized_role == "user":
            user = user_service.demote_admin(target_user_id)
            return user, "updated" if user else "not_found"
        raise ValueError("Unsupported role change. Only admin and user can be assigned from the web panel.")

    def list_admin_plans(self) -> list[dict]:
        with database.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM premium_plans ORDER BY billing_period, created_at DESC, plan_id DESC"
            ).fetchall()
        items = [self._serialize_plan_row(dict(row)) for row in rows]
        return sorted(items, key=lambda item: (self._PLAN_PERIOD_ORDER.get(item["billing_period"], 99), -int(item["plan_id"])))

    def get_admin_plan(self, plan_id: int | None) -> dict | None:
        if not plan_id:
            return None
        with database.connection() as conn:
            row = conn.execute("SELECT * FROM premium_plans WHERE plan_id = ?", (plan_id,)).fetchone()
        return self._serialize_plan_row(dict(row)) if row else None

    def save_admin_plan(
        self,
        *,
        plan_id: int | None,
        name: str,
        price_text: str,
        duration_days: str | int,
        benefits: str | None,
        billing_period: str,
        is_active: bool,
    ) -> dict:
        cleaned_name = " ".join(str(name or "").split())
        if not cleaned_name:
            raise ValueError("Plan name is required.")

        normalized_period = (billing_period or "").strip().lower()
        if normalized_period not in {"monthly", "annual"}:
            raise ValueError("Billing period must be monthly or annual.")

        try:
            amount_rupees = Decimal((price_text or "").strip())
        except InvalidOperation as exc:
            raise ValueError("Plan price must be numeric.") from exc
        if amount_rupees < Decimal("1"):
            raise ValueError("Plan price must be at least 1 INR.")

        try:
            duration_value = int(duration_days)
        except (TypeError, ValueError) as exc:
            raise ValueError("Duration must be a whole number of days.") from exc
        if duration_value <= 0:
            raise ValueError("Duration must be greater than 0 days.")

        amount_paise = int((amount_rupees * Decimal("100")).quantize(Decimal("1")))
        cleaned_benefits = (benefits or "").strip() or None
        timestamp_value = now_iso()

        with database.connection() as conn:
            existing = None
            if plan_id:
                existing = conn.execute("SELECT * FROM premium_plans WHERE plan_id = ?", (plan_id,)).fetchone()
                if not existing:
                    raise ValueError("Plan not found.")
            plan_key = self._build_plan_key(cleaned_name, normalized_period, plan_id=plan_id)
            if existing:
                conn.execute(
                    """
                    UPDATE premium_plans
                    SET plan_key = ?,
                        name = ?,
                        price = ?,
                        duration_days = ?,
                        billing_period = ?,
                        benefits = ?,
                        is_active = ?,
                        updated_at = ?
                    WHERE plan_id = ?
                    """,
                    (
                        plan_key,
                        cleaned_name,
                        amount_paise,
                        duration_value,
                        normalized_period,
                        cleaned_benefits,
                        1 if is_active else 0,
                        timestamp_value,
                        plan_id,
                    ),
                )
                target_plan_id = int(plan_id)
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO premium_plans (
                        plan_key, name, price, duration_days, billing_period, benefits, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan_key,
                        cleaned_name,
                        amount_paise,
                        duration_value,
                        normalized_period,
                        cleaned_benefits,
                        1 if is_active else 0,
                        timestamp_value,
                        timestamp_value,
                    ),
                )
                target_plan_id = int(cursor.lastrowid)
        return self.get_admin_plan(target_plan_id) or {}

    def set_plan_active(self, plan_id: int, is_active: bool) -> dict | None:
        with database.connection() as conn:
            cursor = conn.execute(
                "UPDATE premium_plans SET is_active = ?, updated_at = ? WHERE plan_id = ?",
                (1 if is_active else 0, now_iso(), plan_id),
            )
        if cursor.rowcount <= 0:
            return None
        return self.get_admin_plan(plan_id)

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

    def _serialize_plan_row(self, row: dict) -> dict:
        item = dict(row)
        item["plan_id"] = int(item["plan_id"])
        item["price"] = int(item["price"])
        item["duration_days"] = int(item["duration_days"])
        item["is_active"] = bool(int(item.get("is_active") or 0))
        item["price_rupees"] = Decimal(item["price"]) / Decimal("100")
        item["benefits_lines"] = [line.strip() for line in str(item.get("benefits") or "").splitlines() if line.strip()]
        return item

    def _build_plan_key(self, name: str, billing_period: str, plan_id: int | None = None) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-") or "plan"
        base_key = f"{billing_period}-{slug}"
        with database.connection() as conn:
            candidate = base_key
            suffix = 2
            while True:
                row = conn.execute(
                    "SELECT plan_id FROM premium_plans WHERE plan_key = ?",
                    (candidate,),
                ).fetchone()
                if not row or (plan_id and int(row["plan_id"]) == int(plan_id)):
                    return candidate
                candidate = f"{base_key}-{suffix}"
                suffix += 1

    def _list_users_page(self, conn, *, page: int, date_from: str | None, date_to: str | None) -> tuple[list[dict], dict]:
        where_sql, params = self._date_filter_sql("created_at", date_from, date_to)
        return self._fetch_page(
            conn,
            select_sql=f"SELECT * FROM users {where_sql} ORDER BY created_at DESC, user_id DESC",
            count_sql=f"SELECT COUNT(*) AS count FROM users {where_sql}",
            params=params,
            page=page,
        )

    def _list_payments_page(self, conn, *, page: int, date_from: str | None, date_to: str | None) -> tuple[list[dict], dict]:
        where_sql, params = self._date_filter_sql("timestamp", date_from, date_to)
        return self._fetch_page(
            conn,
            select_sql=f"SELECT * FROM payments {where_sql} ORDER BY timestamp DESC, payment_id DESC",
            count_sql=f"SELECT COUNT(*) AS count FROM payments {where_sql}",
            params=params,
            page=page,
        )

    def _list_orders_page(self, conn, *, page: int, date_from: str | None, date_to: str | None) -> tuple[list[dict], dict]:
        where_sql, params = self._date_filter_sql("po.created_at", date_from, date_to)
        return self._fetch_page(
            conn,
            select_sql=f"""
                SELECT
                    po.*,
                    u.full_name,
                    u.email
                FROM payment_orders po
                LEFT JOIN users u ON u.user_id = po.user_id
                {where_sql}
                ORDER BY po.created_at DESC, po.order_id DESC
            """,
            count_sql=f"SELECT COUNT(*) AS count FROM payment_orders po {where_sql}",
            params=params,
            page=page,
        )

    def _list_support_page(self, conn, *, page: int, date_from: str | None, date_to: str | None) -> tuple[list[dict], dict]:
        where_sql, params = self._date_filter_sql("sm.created_at", date_from, date_to)
        return self._fetch_page(
            conn,
            select_sql=f"""
                SELECT
                    sm.*,
                    u.full_name AS account_full_name,
                    u.email
                FROM support_messages sm
                LEFT JOIN users u ON u.user_id = sm.user_id
                {where_sql}
                ORDER BY sm.created_at DESC, sm.support_id DESC
            """,
            count_sql=f"SELECT COUNT(*) AS count FROM support_messages sm {where_sql}",
            params=params,
            page=page,
        )

    def _list_reports_page(self, conn, *, page: int, date_from: str | None, date_to: str | None) -> tuple[list[dict], dict]:
        where_sql, params = self._date_filter_sql("qr.created_at", date_from, date_to)
        return self._fetch_page(
            conn,
            select_sql=f"""
                SELECT
                    qr.*,
                    u.full_name,
                    q.question_text,
                    s.title AS set_title,
                    e.title AS exam_title
                FROM question_reports qr
                LEFT JOIN users u ON u.user_id = qr.user_id
                LEFT JOIN questions q ON q.question_id = qr.question_id
                LEFT JOIN exam_sets s ON s.set_id = qr.set_id
                LEFT JOIN exams e ON e.exam_id = s.exam_id
                {where_sql}
                ORDER BY qr.created_at DESC, qr.report_id DESC
            """,
            count_sql=f"SELECT COUNT(*) AS count FROM question_reports qr {where_sql}",
            params=params,
            page=page,
        )

    def _list_user_logs_page(self, conn, *, page: int, date_from: str | None, date_to: str | None) -> tuple[list[dict], dict]:
        where_sql, params = self._date_filter_sql("qa.created_at", date_from, date_to)
        return self._fetch_page(
            conn,
            select_sql=f"""
                SELECT
                    qa.attempt_id,
                    qa.user_id,
                    qa.created_at,
                    qa.requested_count,
                    qa.correct_count,
                    qa.wrong_count,
                    qa.skipped_count,
                    qa.ended_reason,
                    u.full_name,
                    s.title AS set_title,
                    e.title AS exam_title
                FROM quiz_attempts qa
                LEFT JOIN users u ON u.user_id = qa.user_id
                LEFT JOIN exam_sets s ON s.set_id = qa.set_id
                LEFT JOIN exams e ON e.exam_id = s.exam_id
                {where_sql}
                ORDER BY qa.created_at DESC, qa.attempt_id DESC
            """,
            count_sql=f"SELECT COUNT(*) AS count FROM quiz_attempts qa {where_sql}",
            params=params,
            page=page,
        )

    def _date_filter_sql(self, column_name: str, date_from: str | None, date_to: str | None) -> tuple[str, tuple[str, ...]]:
        clauses: list[str] = []
        params: list[str] = []
        if (date_from or "").strip():
            clauses.append(f"{column_name} >= ?")
            params.append(f"{date_from.strip()}T00:00:00")
        if (date_to or "").strip():
            clauses.append(f"{column_name} <= ?")
            params.append(f"{date_to.strip()}T23:59:59")
        if not clauses:
            return "", tuple()
        return "WHERE " + " AND ".join(clauses), tuple(params)

    def _fetch_page(self, conn, *, select_sql: str, count_sql: str, params: tuple | list, page: int, per_page: int = 10) -> tuple[list[dict], dict]:
        normalized_page = self._coerce_page(page)
        total_items = int(conn.execute(count_sql, tuple(params)).fetchone()["count"] or 0)
        total_pages = max(math.ceil(total_items / per_page), 1) if total_items else 1
        if normalized_page > total_pages:
            normalized_page = total_pages
        offset = (normalized_page - 1) * per_page
        rows = conn.execute(f"{select_sql} LIMIT ? OFFSET ?", tuple(params) + (per_page, offset)).fetchall()
        return [dict(row) for row in rows], self._build_pagination(total_items, normalized_page, per_page)

    def _paginate_list(self, items: list[dict], *, page: int, per_page: int = 10) -> tuple[list[dict], dict]:
        pagination = self._build_pagination(len(items), self._coerce_page(page), per_page)
        start = (pagination["page"] - 1) * per_page
        end = start + per_page
        return items[start:end], pagination

    def _build_pagination(self, total_items: int, page: int, per_page: int) -> dict:
        total_pages = max(math.ceil(total_items / per_page), 1) if total_items else 1
        normalized_page = min(max(page, 1), total_pages)
        start_page = max(1, normalized_page - 2)
        end_page = min(total_pages, normalized_page + 2)
        if end_page - start_page < 4:
            start_page = max(1, end_page - 4)
            end_page = min(total_pages, start_page + 4)
        return {
            "page": normalized_page,
            "per_page": per_page,
            "total_items": total_items,
            "total_pages": total_pages,
            "pages": list(range(start_page, end_page + 1)),
            "has_prev": normalized_page > 1,
            "has_next": normalized_page < total_pages,
            "prev_page": normalized_page - 1,
            "next_page": normalized_page + 1,
        }

    def _empty_pagination(self, per_page: int) -> dict:
        return self._build_pagination(0, 1, per_page)

    def _coerce_page(self, value) -> int:
        try:
            page = int(value)
        except (TypeError, ValueError):
            return 1
        return max(page, 1)

    def _dashboard_cache_key(self, *parts):
        return tuple(parts)

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
