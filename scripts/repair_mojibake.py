from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import connect
from psycopg.rows import dict_row

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT_DIR / "data" / "repair_backups"
TABLE_NAME = "questions"
ID_COLUMN = "question_id"
TEXT_FIELDS = [
    "question_text",
    "option_a",
    "option_b",
    "option_c",
    "option_d",
    "correct_option",
    "explanation",
]
MOJIBAKE_MARKERS = ("\u00e0\u00a4", "\u00e0\u00a5")


def utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def load_database_url() -> str:
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set in the environment.")
    if database_url.startswith("postgres://"):
        return "postgresql://" + database_url[len("postgres://"):]
    return database_url


def contains_mojibake(value: Any) -> bool:
    return isinstance(value, str) and any(marker in value for marker in MOJIBAKE_MARKERS)


def repair_text(value: str) -> str:
    return value.encode("latin1").decode("utf-8")


def fetch_affected_rows(conn) -> list[dict[str, Any]]:
    where_clauses = []
    params: list[str] = []
    for field_name in TEXT_FIELDS:
        for marker in MOJIBAKE_MARKERS:
            where_clauses.append(f"{field_name} LIKE %s")
            params.append(f"%{marker}%")

    query = (
        f"SELECT {ID_COLUMN}, " + ", ".join(TEXT_FIELDS) + f" FROM {TABLE_NAME} "
        + "WHERE " + " OR ".join(where_clauses)
        + f" ORDER BY {ID_COLUMN}"
    )
    with conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def export_rows(rows: list[dict[str, Any]]) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"questions_mojibake_backup_{utc_timestamp()}.json"
    payload = {
        "table": TABLE_NAME,
        "id_column": ID_COLUMN,
        "text_fields": TEXT_FIELDS,
        "row_count": len(rows),
        "rows": rows,
    }
    backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup_path


def build_changes(row: dict[str, Any]) -> dict[str, str]:
    changes: dict[str, str] = {}
    for field_name in TEXT_FIELDS:
        value = row.get(field_name)
        if not contains_mojibake(value):
            continue
        try:
            repaired = repair_text(value)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != value:
            changes[field_name] = repaired
    return changes


def apply_repairs(conn, rows: list[dict[str, Any]]) -> tuple[int, int]:
    updated_rows = 0
    updated_fields = 0
    with conn.cursor() as cur:
        for row in rows:
            changes = build_changes(row)
            if not changes:
                continue

            assignments = ", ".join(f"{field_name} = %s" for field_name in changes)
            params = [changes[field_name] for field_name in changes]
            params.append(row[ID_COLUMN])
            cur.execute(
                f"UPDATE {TABLE_NAME} SET {assignments} WHERE {ID_COLUMN} = %s",
                params,
            )
            updated_rows += 1
            updated_fields += len(changes)

    conn.commit()
    return updated_rows, updated_fields


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    database_url = load_database_url()

    with connect(database_url, row_factory=dict_row) as conn:
        affected_rows = fetch_affected_rows(conn)
        backup_path = export_rows(affected_rows)

        print(f"affected_rows={len(affected_rows)}")
        print(f"backup_path={backup_path}")

        if dry_run:
            print("dry_run=True")
            return 0

        updated_rows, updated_fields = apply_repairs(conn, affected_rows)
        print(f"updated_rows={updated_rows}")
        print(f"updated_fields={updated_fields}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
