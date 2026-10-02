"""
sales-sync'ning O'Z, kichik holat bazasi — Analytics yoki Ombor bazasiga
hech qanday aloqasi yo'q. Qaysi reaksiya hodisalari allaqachon ko'rib
chiqilganini eslab qolish uchun (Ombor'ning source_id-asosidagi
idempotentligi ustiga qo'shimcha himoya qatlami).
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_reactions (
    sync_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('SYNCED', 'FAILED', 'NEEDS_REVIEW', 'NOT_AN_ORDER')),
    reason TEXT,
    processed_at TEXT NOT NULL,
    first_seen_at TEXT
);
"""


@dataclass(frozen=True)
class RetryItem:
    sync_key: str
    chat_id: int
    message_id: int
    status: str


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
        # Eski bazalar (first_seen_at'dan oldingi) - ustunni qo'shamiz
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(processed_reactions)")}
        if "first_seen_at" not in cols:
            self._conn.execute("ALTER TABLE processed_reactions ADD COLUMN first_seen_at TEXT")
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

    def list_retryable(self, *, max_age_days: int, limit: int) -> list[RetryItem]:
        """Avtomatik qayta urinish uchun: FAILED (Ombor xatosi/tarmoq) va
        NEEDS_REVIEW (masalan Ombor'da bog'lanmagan kod - owner bog'lasa o'tadi).
        Birinchi ko'rilganidan max_age_days o'tganlari - endi urinilmaydi."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        rows = self._conn.execute(
            "SELECT sync_key, status FROM processed_reactions "
            "WHERE status IN ('FAILED', 'NEEDS_REVIEW') "
            "AND COALESCE(first_seen_at, processed_at) >= ? "
            "ORDER BY processed_at LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        out = []
        for row in rows:
            chat_raw, _, msg_raw = row["sync_key"].rpartition(":")
            try:
                out.append(RetryItem(row["sync_key"], int(chat_raw), int(msg_raw), row["status"]))
            except ValueError:
                continue
        return out

    def _upsert(self, sync_key, *, status, reason) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # first_seen_at faqat birinchi yozilganda (ON CONFLICT da o'zgarmaydi)
        self._conn.execute(
            """
            INSERT INTO processed_reactions (sync_key, status, reason, processed_at, first_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sync_key) DO UPDATE SET
                status=excluded.status, reason=excluded.reason, processed_at=excluded.processed_at
            """,
            (sync_key, status, reason, now, now),
        )
        self._conn.commit()
