"""Avtomatik sotuv: Analytics QABUL QILGAN har buyurtma Ombor'ga - reaksiya kutilmaydi.

Kuniga BIR MARTA, kun oxirida (AUTO_SYNC_TIME, standart 23:50, BUSINESS_TIMEZONE - Asia/Seoul):
owner (2026-10-21) "har 10 daqiqa shart emas, kun oxirida". Shu kun sotuvi Ombor'ga o'sha kun
sanasi bilan tushadi. Servis o'sha vaqtda ishlamagan bo'lsa - ishga tushganda o'tkazib yuborilgan
kun darhol bajariladi.

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


LAST_RUN_KEY = "auto_sync_last_day"


def parse_run_time(raw: str):
    from datetime import time
    raw = (raw or "").strip().lower()
    if raw in ("off", "0", "no", "false"):
        return None
    try:
        hh, mm = (raw or "23:50").split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return time(23, 50)


def next_run(now_local: datetime, run_at, last_day: str | None) -> datetime:
    """Keyingi ishga tushish (mahalliy vaqt). Bugungi vaqt o'tgan va bugun hali bajarilmagan
    bo'lsa - darhol (o'tkazib yuborilgan kun)."""
    today_run = now_local.replace(hour=run_at.hour, minute=run_at.minute, second=0, microsecond=0)
    if now_local >= today_run and last_day != now_local.date().isoformat():
        return now_local
    if now_local < today_run:
        return today_run
    return today_run + timedelta(days=1)


async def run_daily_loop(config, analytics, ombor, state, notify=None, *, sleep=None, clock=None,
                         max_runs: int | None = None) -> None:
    import asyncio
    import os
    from zoneinfo import ZoneInfo
    sleep = sleep or asyncio.sleep
    tz = ZoneInfo(os.getenv("BUSINESS_TIMEZONE", "Asia/Seoul"))
    run_at = parse_run_time(os.getenv("AUTO_SYNC_TIME", "23:50"))
    if run_at is None or getattr(config, "auto_sync_lookback_days", 0) <= 0:
        logger.info("Avtomatik sotuv o'chiq")
        return
    runs = 0
    while max_runs is None or runs < max_runs:
        now_local = (clock() if clock else datetime.now(timezone.utc)).astimezone(tz)
        target = next_run(now_local, run_at, state.get_meta(LAST_RUN_KEY))
        await sleep(max(0.0, (target - now_local).total_seconds()))
        runs += 1
        try:
            a = await auto_sync_once(config, analytics, ombor, state, notify,
                                     lookback_days=config.auto_sync_lookback_days)
            state.set_meta(LAST_RUN_KEY, target.date().isoformat())
            logger.info("Kunlik avtomatik sotuv (%s): ko'rildi=%d, yozildi=%d, muammo=%d",
                        target.date(), a.seen, a.synced, a.problems)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keyingi kunda qayta
            logger.exception("Kunlik avtomatik sotuvda xato")
            await sleep(600)   # 10 daqiqadan keyin qayta urinish (shu kun hali bajarilmagan)
