"""
sales-sync'ning O'Z, kichik holat bazasi — Analytics yoki Ombor bazasiga
hech qanday aloqasi yo'q. Qaysi reaksiya hodisalari allaqachon ko'rib
chiqilganini eslab qolish uchun (Ombor'ning source_id-asosidagi
idempotentligi ustiga qo'shimcha himoya qatlami).
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_reactions (
    sync_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('SYNCED', 'FAILED', 'NEEDS_REVIEW', 'NOT_AN_ORDER')),
    reason TEXT,
    processed_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ReviewItem:
    sync_key: str
    reason: str
    processed_at: str


class StateStore:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_synced_keys(self) -> set[str]:
        """Faqat SYNCED chetlab o'tiladi. FAILED/NEEDS_REVIEW qayta uriniladi
        (owner Ombor'da kodni bog'lasa yoki Ombor tuzalsa, o'zi
        tuzalib ketishi uchun)."""
        rows = self._conn.execute(
            "SELECT sync_key FROM processed_reactions WHERE status = 'SYNCED'"
        ).fetchall()
        return {row["sync_key"] for row in rows}

    def mark_synced(self, sync_key: str) -> None:
        self._upsert(sync_key, status="SYNCED", reason=None)

    def mark_failed(self, sync_key: str, *, reason: str) -> None:
        self._upsert(sync_key, status="FAILED", reason=reason)

    def mark_needs_review(self, sync_key: str, *, reason: str) -> None:
        self._upsert(sync_key, status="NEEDS_REVIEW", reason=reason)

    def mark_not_an_order(self, sync_key: str) -> None:
        # Oddiy suhbat xabari — qayta tekshirmaslik uchun eslab qolamiz
        # (lekin kerak bo'lsa qayta ishlash mumkin, chetlab o'tishga majburlamaydi).
        self._upsert(sync_key, status="NOT_AN_ORDER", reason=None)

    def get_status(self, sync_key: str) -> tuple[str, str | None] | None:
        """(status, reason) yoki None - bu xabar hali ko'rilmagan bo'lsa."""
        row = self._conn.execute(
            "SELECT status, reason FROM processed_reactions WHERE sync_key = ?", (sync_key,)
        ).fetchone()
        return (row["status"], row["reason"]) if row else None

    def list_needs_review(self) -> list[ReviewItem]:
        rows = self._conn.execute(
            "SELECT sync_key, reason, processed_at FROM processed_reactions "
            "WHERE status = 'NEEDS_REVIEW' ORDER BY processed_at"
        ).fetchall()
        return [
            ReviewItem(sync_key=r["sync_key"], reason=r["reason"], processed_at=r["processed_at"])
            for r in rows
        ]

    def _upsert(self, sync_key, *, status, reason) -> None:
        self._conn.execute(
            """
            INSERT INTO processed_reactions (sync_key, status, reason, processed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sync_key) DO UPDATE SET
                status=excluded.status, reason=excluded.reason, processed_at=excluded.processed_at
            """,
            (sync_key, status, reason, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
