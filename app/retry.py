"""Rad etilgan / ko'rib chiqish kerak bo'lgan sotuvlarni avtomatik qayta yuborish.

Avval FAILED (Ombor ishlamadi, tarmoq) yoki NEEDS_REVIEW (masalan Ombor'da
bog'lanmagan kod) buyurtma faqat guruhda reaksiya qayta qo'yilganda qayta
yuborilardi - aks holda tayyor mahsulot hisobi to'liq bo'lmasdi.

Endi har SALES_RETRY_INTERVAL_MINUTES (standart 10, 0 - o'chiq) daqiqada
shunday buyurtmalar o'zi qayta tekshiriladi (birinchi ko'rilganidan
SALES_RETRY_MAX_AGE_DAYS - standart 7 kun - ichida). Ombor tiklansa yoki
owner kodni Ombor'da bog'lasa - buyurtma o'zi yoziladi.

Takror yozuv xavfi yo'q: Ombor source_id bo'yicha idempotent. Ogohlantirishlar
takrorlanmaydi: sabab o'zgarmasa - xabar yo'q (Ombor va _needs_review mantiqi).
"""
import asyncio
import logging
from dataclasses import dataclass

from app.sync_logic import alert_for_result, process_reaction

logger = logging.getLogger(__name__)

RETRY_BATCH_LIMIT = 50


@dataclass
class RetrySummary:
    checked: int = 0
    synced: int = 0
    still_failing: int = 0


async def retry_once(config, analytics, ombor, state, notify=None) -> RetrySummary:
    """Bitta aylanish. notify(chat_id, message_id, result) - needs_review xabari uchun (bot)."""
    summary = RetrySummary()
    for item in state.list_retryable(max_age_days=config.retry_max_age_days, limit=RETRY_BATCH_LIMIT):
        summary.checked += 1
        try:
            result = await process_reaction(item.chat_id, item.message_id, config, analytics, ombor, state)
        except Exception:  # noqa: BLE001 - bitta buyurtma qolganlarini to'xtatmasin
            logger.exception("Qayta urinishda xato: %s", item.sync_key)
            summary.still_failing += 1
            continue
        if result.outcome == "synced":
            summary.synced += 1
            logger.info("Qayta urinish: %s yozildi (buyurtma %s)", item.sync_key, result.order_id)
        elif result.outcome in ("failed", "needs_review"):
            summary.still_failing += 1
        await alert_for_result(ombor, item.chat_id, item.message_id, result)
        if result.outcome == "needs_review" and result.notify and notify is not None:
            await notify(item.chat_id, item.message_id, result)
    return summary


async def run_retry_loop(config, analytics, ombor, state, notify=None, *, sleep=asyncio.sleep,
                         max_cycles: int | None = None) -> None:
    if config.retry_interval_minutes <= 0:
        logger.info("Avtomatik qayta urinish o'chiq (SALES_RETRY_INTERVAL_MINUTES=0)")
        return
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        await sleep(config.retry_interval_minutes * 60)
        try:
            s = await retry_once(config, analytics, ombor, state, notify)
            if s.checked:
                logger.info("Qayta urinish: tekshirildi=%d, yozildi=%d, hali muammo=%d",
                            s.checked, s.synced, s.still_failing)
            # C: yuborilgan buyurtmalar Analytics'da o'zgarganmi - farq Ombor'ga
            from app.corrections import correct_once
            c = await correct_once(analytics, ombor, state)
            if c.corrected or c.failed:
                logger.info("Tuzatishlar: tekshirildi=%d, tuzatildi=%d, xato=%d", c.checked, c.corrected, c.failed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - sikl to'xtamasin
            logger.exception("Qayta urinish siklida xato")
