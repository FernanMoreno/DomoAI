"""SQLite-backed household-scoped privacy storage."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from domoai.domain.privacy import PrivacyCategory
from domoai.persistence.sqlite import SQLiteDatabase

_PAYLOAD_TABLES: dict[PrivacyCategory, tuple[tuple[str, str, str], ...]] = {
    PrivacyCategory.STATE: (("state_snapshots", "payload", "payload"),),
    PrivacyCategory.PLANS: (("plans", "payload", "payload"),),
    PrivacyCategory.APPROVALS: (("approval_grants", "payload", "payload"),),
    PrivacyCategory.BUNDLES: (("bundle_commits", "payload", "payload"),),
    PrivacyCategory.SCHEDULES: (
        ("scheduled_plans", "payload", "payload"),
        ("recurring_schedules", "template_payload", "authority_payload"),
    ),
    PrivacyCategory.AUTOMATIONS: (("automation_rules", "payload", "payload"),),
}


class SQLitePrivacyStore:
    """Read/delete only rows whose canonical payload names the requested home."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def export_category(
        self, category: PrivacyCategory, household_id: str
    ) -> list[dict[str, Any]]:
        tables = _PAYLOAD_TABLES.get(category)
        if tables is None:
            raise ValueError(f"privacy category {category.value!r} is not stored as household data")
        records: list[dict[str, Any]] = []
        for table, payload_column, authority_column in tables:
            if table == "recurring_schedules":
                cursor = self.database.connection.execute(
                    f"""SELECT schedule_id, template_payload, recurrence_payload,
                              next_execute_at, status, updated_at, authority_payload
                       FROM recurring_schedules
                       WHERE {_household_predicate("authority_payload")}""",
                    (household_id, household_id),
                )
                records.extend(
                    {
                        "schedule_id": row[0],
                        "template": json.loads(row[1]),
                        "recurrence": json.loads(row[2]),
                        "next_execute_at": row[3],
                        "status": row[4],
                        "updated_at": row[5],
                        "authority": json.loads(row[6] or "{}"),
                    }
                    for row in cursor.fetchall()
                )
                cursor.close()
                continue
            cursor = self.database.connection.execute(
                f"""SELECT {payload_column} FROM {table}
                    WHERE {_household_predicate(authority_column)}""",
                (household_id, household_id),
            )
            records.extend(json.loads(row[0]) for row in cursor.fetchall())
            cursor.close()
        if category is PrivacyCategory.STATE:
            cursor = self.database.connection.execute(
                """SELECT history_id, payload, observed_at, received_at
                   FROM state_history WHERE household_id = ?
                   ORDER BY julianday(observed_at), history_id""",
                (household_id,),
            )
            records.extend(
                {
                    "history_id": row[0],
                    "snapshot": json.loads(row[1]),
                    "observed_at": row[2],
                    "received_at": row[3],
                }
                for row in cursor.fetchall()
            )
            cursor.close()
        return records

    async def delete_category(self, category: PrivacyCategory, household_id: str) -> int:
        counts = await self.delete_categories([category], household_id)
        return counts[category.value]

    async def count_category(self, category: PrivacyCategory, household_id: str) -> int:
        tables = _PAYLOAD_TABLES.get(category)
        if tables is None:
            raise ValueError(f"privacy category {category.value!r} is not stored as household data")
        count = 0
        for table, _payload_column, authority_column in tables:
            cursor = self.database.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {_household_predicate(authority_column)}",
                (household_id, household_id),
            )
            count += int(cursor.fetchone()[0])
            cursor.close()
        if category is PrivacyCategory.STATE:
            cursor = self.database.connection.execute(
                "SELECT COUNT(*) FROM state_history WHERE household_id = ?",
                (household_id,),
            )
            count += int(cursor.fetchone()[0])
            cursor.close()
        return count

    async def export_category_page(
        self,
        category: PrivacyCategory,
        household_id: str,
        *,
        offset: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        if offset < 0 or limit < 1:
            raise ValueError("privacy export page bounds are invalid")
        tables = _PAYLOAD_TABLES.get(category)
        if tables is None:
            raise ValueError(f"privacy category {category.value!r} is not stored as household data")
        records: list[dict[str, Any]] = []
        skipped = offset
        for table, _payload_column, authority_column in tables:
            table_count = await self._count_table(table, authority_column, household_id)
            if skipped >= table_count:
                skipped -= table_count
                continue
            page_limit = limit - len(records)
            if table == "recurring_schedules":
                cursor = self.database.connection.execute(
                    f"""SELECT schedule_id, template_payload, recurrence_payload,
                              next_execute_at, status, updated_at, authority_payload
                       FROM recurring_schedules
                       WHERE {_household_predicate("authority_payload")}
                       ORDER BY schedule_id
                       LIMIT ? OFFSET ?""",
                    (household_id, household_id, page_limit, skipped),
                )
                rows = cursor.fetchmany(page_limit)
                records.extend(
                    {
                        "schedule_id": row[0],
                        "template": json.loads(row[1]),
                        "recurrence": json.loads(row[2]),
                        "next_execute_at": row[3],
                        "status": row[4],
                        "updated_at": row[5],
                        "authority": json.loads(row[6] or "{}"),
                    }
                    for row in rows
                )
                cursor.close()
            else:
                cursor = self.database.connection.execute(
                    f"""SELECT {_payload_column} FROM {table}
                        WHERE {_household_predicate(authority_column)}
                        ORDER BY rowid
                        LIMIT ? OFFSET ?""",
                    (household_id, household_id, page_limit, skipped),
                )
                records.extend(json.loads(row[0]) for row in cursor.fetchmany(page_limit))
                cursor.close()
            skipped = 0
            if len(records) >= limit:
                return records[:limit]
        if category is PrivacyCategory.STATE and len(records) < limit:
            history_cursor = self.database.connection.execute(
                "SELECT COUNT(*) FROM state_history WHERE household_id = ?",
                (household_id,),
            )
            history_count = int(history_cursor.fetchone()[0])
            history_cursor.close()
            if skipped < history_count:
                page_limit = limit - len(records)
                cursor = self.database.connection.execute(
                    """SELECT history_id, payload, observed_at, received_at
                       FROM state_history
                       WHERE household_id = ?
                       ORDER BY julianday(observed_at), history_id
                       LIMIT ? OFFSET ?""",
                    (household_id, page_limit, skipped),
                )
                records.extend(
                    {
                        "history_id": row[0],
                        "snapshot": json.loads(row[1]),
                        "observed_at": row[2],
                        "received_at": row[3],
                    }
                    for row in cursor.fetchmany(page_limit)
                )
                cursor.close()
        return records[:limit]

    async def delete_categories(
        self, categories: Sequence[PrivacyCategory], household_id: str
    ) -> dict[str, int]:
        for category in categories:
            if category not in _PAYLOAD_TABLES:
                raise ValueError(
                    f"privacy category {category.value!r} is not stored as household data"
                )
        deleted_counts: dict[str, int] = {}
        try:
            for category in categories:
                deleted = 0
                tables = _PAYLOAD_TABLES[category]
                for table, _payload_column, authority_column in tables:
                    cursor = self.database.connection.execute(
                        f"""DELETE FROM {table}
                            WHERE {_household_predicate(authority_column)}""",
                        (household_id, household_id),
                    )
                    deleted += cursor.rowcount
                if category is PrivacyCategory.STATE:
                    cursor = self.database.connection.execute(
                        "DELETE FROM state_history WHERE household_id = ?",
                        (household_id,),
                    )
                    deleted += cursor.rowcount
                deleted_counts[category.value] = deleted
            self.database.connection.commit()
        except Exception:
            self.database.connection.rollback()
            raise
        return deleted_counts

    async def _count_table(
        self, table: str, authority_column: str, household_id: str
    ) -> int:
        cursor = self.database.connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {_household_predicate(authority_column)}",
            (household_id, household_id),
        )
        count = int(cursor.fetchone()[0])
        cursor.close()
        return count


def _household_predicate(authority_column: str) -> str:
    path = (
        "$.authority.household_id"
        if authority_column == "payload"
        else "$.household_id"
    )
    # Rows written before Phase 3 have no authority field. They remain
    # readable under the additive legacy/default household, but never become
    # visible to a named household.
    return (
        f"(json_extract({authority_column}, '{path}') = ? "
        f"OR (? = 'default' AND json_extract({authority_column}, '{path}') IS NULL))"
    )


__all__ = ["SQLitePrivacyStore"]
