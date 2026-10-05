"""C: Analytics'da buyurtma o'zgarsa/o'chirilsa - Ombor ham tuzatiladi.

Avval sales-sync buyurtmani reaksiya paytida BIR MARTA yuborardi; keyingi
tuzatish ("+2 non" bonus), qayta tahlil yoki o'chirish Ombor'ga yetmasdi va
tayyor mahsulot qoldig'i asta-sekin noto'g'ri bo'lardi.

Har aylanishda (qayta urinish bilan birga) oxirgi WATCH_DAYS kunda yuborilgan
buyurtmalar Analytics'dan qayta o'qiladi (API yoki baza - qaysi ulangan bo'lsa).
Hozirgi kerakli miqdor (asosiy + bonus; o'chirilgan/qabul qilinmagan - 0) bilan
yuborilgani farq qilsa - farq Ombor'ga tuzatish bo'lib boradi
(POST /sales-shipments/corrections/by-mapping: + qo'shimcha chiqim, - qaytish).
source_id = maqsad holat barmoq izi - takror yuborish xavfsiz.
"""
import hashlib
import json
import logging
from dataclasses import dataclass

from erp_bridge_kit import BridgeError
from erp_bridge_kit.ombor import unmapped_codes

from app.analytics_reader import desired_quantities
from app.sync_logic import ANALYTICS_SYSTEM, send_alert

logger = logging.getLogger(__name__)
WATCH_DAYS = 3


@dataclass
class CorrectionSummary:
    checked: int = 0
    corrected: int = 0
    failed: int = 0


def diff(pushed: dict[str, int], desired: dict[str, int]) -> dict[str, int]:
    codes = set(pushed) | set(desired)
    return {c: desired.get(c, 0) - pushed.get(c, 0) for c in sorted(codes) if desired.get(c, 0) != pushed.get(c, 0)}


def target_fingerprint(desired: dict[str, int]) -> str:
    raw = json.dumps(desired, sort_keys=True) if desired else "cancelled"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _split_key(sync_key: str) -> tuple[int, int]:
    chat, _, message = sync_key.rpartition(":")
    return int(chat), int(message)


async def correct_once(analytics, ombor, state, *, watch_days: int = WATCH_DAYS) -> CorrectionSummary:
    summary = CorrectionSummary()
    for sync_key, order_id, pushed in state.list_watched(max_age_days=watch_days):
        summary.checked += 1
        chat_id, message_id = _split_key(sync_key)
        try:
            order = await analytics.fetch_order_by_message(chat_id, message_id)
        except Exception as e:  # noqa: BLE001 - Analytics vaqtincha ishlamasa keyingi aylanishda
            logger.warning("Tuzatish: %s ni o'qib bo'lmadi: %s", sync_key, type(e).__name__)
            summary.failed += 1
            continue
        desired = desired_quantities(order)
        delta = diff(pushed, desired)
        if not delta:
            continue
        what = "bekor qilindi" if not desired else "o'zgardi"
        try:
            await ombor.push_sales_correction_by_mapping(
                source_id=f"analytics-order:{order_id}:rev:{target_fingerprint(desired)}",
                system=ANALYTICS_SYSTEM, order_reference=str(order_id),
                items=[{"code": c, "quantity": str(q)} for c, q in delta.items()],
            )
        except BridgeError as e:
            summary.failed += 1
            missing = unmapped_codes(e)
            reason = (f"bog'lanmagan kod: {', '.join(missing)}" if missing else str(e))[:200]
            logger.warning("Tuzatish: buyurtma %s Ombor'ga yozilmadi: %s", order_id, reason)
            await send_alert(ombor, f"sales-sync:correction:{order_id}", "warning",
                             f"Sotuv: buyurtma {order_id} Analytics'da {what}, lekin Ombor tuzatilmadi: {reason}. "
                             "Avtomatik qayta urinilmoqda.")
            continue
        state.record_pushed(sync_key, order_id, desired)
        summary.corrected += 1
        logger.info("Tuzatish: buyurtma %s %s - Ombor'ga %s", order_id, what, delta)
        await send_alert(ombor, f"sales-sync:correction:{order_id}", "recovered",
                         f"Sotuv: buyurtma {order_id} Analytics'da {what} - Ombor tuzatildi: "
                         + ", ".join(f"{c} {q:+d}" for c, q in delta.items()))
    return summary
