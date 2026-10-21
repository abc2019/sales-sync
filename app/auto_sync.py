"""Avtomatik sotuv: Analytics QABUL QILGAN har buyurtma Ombor'ga - reaksiya kutilmaydi.

Owner (2026-10-21): guruhda reaksiya bosilgan buyurtmalar ham Ombor'ga tushmayotgan edi
(Telegram reaksiya hodisasini botga faqat u guruhda admin bo'lsa va har doim ham yubormaydi).
"Analytics'dan olsa bo'lmaydimi" - endi har aylanishda Analytics'ning
GET /internal/orders/changes?since= orqali yangi/o'zgargan buyurtmalar o'qiladi va
qabul qilingani (Analytics'ning yagona qoidasi) Ombor'ga yuboriladi.

- Takror yozuv yo'q: source_id = analytics-order:{id} (Ombor idempotent) + mahalliy SYNCED.
- Faqat oxirgi AUTO_SYNC_LOOKBACK_DAYS (standart 3) kun ichida YARATILGAN buyurtmalar
  (eski buyurtma tahrirlansa - u allaqachon tuzatishlar (corrections) bilan kuzatiladi).
- Reaksiya yo'li ham ishlayveradi (bir xil natija, takror yo'q).
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.analytics_reader import OrderItem, OrderRecord
from app.sync_logic import process_order

logger = logging.getLogger(__name__)
PAGE_LIMIT = 200
MAX_PAGES = 20
WATERMARK_KEY = "auto_sync_since"


@dataclass
class AutoSyncSummary:
    seen: int = 0
    synced: int = 0
    skipped: int = 0
    problems: int = 0


def order_from_payload(data: dict) -> OrderRecord:
    return OrderRecord(
        order_id=int(data["order_id"]),
        items=tuple(OrderItem(product_code=i["product"], units_total=int(i["units_total"]))
                    for i in data.get("items") or []),
        accepted=bool(data.get("accepted")) and not data.get("is_deleted"),
        review_status=str(data.get("review_status") or "NONE"),
        bonus_items=tuple(OrderItem(product_code=b["product"], units_total=int(b["quantity"]))
                          for b in data.get("bonus_items") or []),
    )


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def auto_sync_once(config, analytics, ombor, state, notify=None, *, now: datetime | None = None,
                         lookback_days: int = 3) -> AutoSyncSummary:
    summary = AutoSyncSummary()
    fetch = getattr(analytics, "fetch_changes", None)
    if fetch is None:
        return summary   # Analytics bazaga ulangan (eski yo'l) - faqat reaksiya
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    since = _parse(state.get_meta(WATERMARK_KEY)) or cutoff
    since = max(since - timedelta(seconds=5), cutoff - timedelta(days=1))   # chegaradagi bir xil vaqtlar
    synced_keys = state.get_synced_keys()
    newest = since
    for _ in range(MAX_PAGES):
        page = await fetch(since, PAGE_LIMIT)
        if not page:
            break
        for data in page:
            summary.seen += 1
            changed = _parse(data.get("changed_at")) or since
            newest = max(newest, changed)
            created = _parse(data.get("created_at"))
            sync_key = f"{int(data['chat_id'])}:{int(data['message_id'])}"
            order = order_from_payload(data)
            if (created is not None and created < cutoff) or not order.accepted or sync_key in synced_keys:
                summary.skipped += 1
                continue
            result = await process_order(int(data["chat_id"]), int(data["message_id"]), order, ombor, state)
            if result.outcome == "synced":
                summary.synced += 1
                synced_keys.add(sync_key)
            else:
                summary.problems += 1
                if result.outcome == "needs_review" and result.notify and notify is not None:
                    await notify(int(data["chat_id"]), int(data["message_id"]), result)
        if len(page) < PAGE_LIMIT:
            break
        since = newest
    state.set_meta(WATERMARK_KEY, newest.isoformat())
    return summary
