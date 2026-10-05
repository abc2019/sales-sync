import logging
from dataclasses import dataclass

from erp_bridge_kit.ombor import unmapped_codes
from erp_bridge_kit import BridgeError, OmborBridgeClient, build_source_id

from app.analytics_reader import AnalyticsReader
from app.config import Config
from app.state import StateStore

logger = logging.getLogger(__name__)


@dataclass
class ReactionResult:
    outcome: str  # "not_an_order" | "already_synced" | "needs_review" | "synced" | "failed" | "ombor_not_configured"
    reason: str | None = None
    order_id: int | None = None
    # needs_review birinchi marta (yoki sababi o'zgarib) aniqlandi - OWNER'ga
    # xabar yuborish kerak. Qayta reaksiyada bir xil sabab bilan - takror yo'q.
    notify: bool = False


ANALYTICS_SYSTEM = "analytics"  # Ombor /product-mappings/{system}


def _sync_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _needs_review(state: StateStore, sync_key: str, reason: str, order_id: int | None) -> "ReactionResult":
    previous = state.get_status(sync_key)
    state.mark_needs_review(sync_key, reason=reason)
    return ReactionResult(
        outcome="needs_review", reason=reason, order_id=order_id,
        notify=previous != ("NEEDS_REVIEW", reason),
    )


def message_link(chat_id: int, message_id: int) -> str | None:
    """Supergroup xabariga havola (t.me/c/...). Oddiy guruhda - None."""
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return None


def build_review_message(chat_id: int, message_id: int, result: "ReactionResult") -> str:
    lines = [
        "⚠️ Buyurtma Ombor'ga yozilmadi - ko'rib chiqish kerak",
        f"Sabab: {result.reason}",
    ]
    if result.order_id is not None:
        lines.append(f"Buyurtma: #{result.order_id}")
    link = message_link(chat_id, message_id)
    lines.append(f"Xabar: {link}" if link else f"Xabar ID: {message_id}")
    lines += [
        "",
        "Tuzatish: Ombor botida ⚙️ Sozlamalar → 🔗 Mahsulot kodlari, keyin xabardagi "
        "reaksiyani olib, qayta qo'ying: buyurtma qayta yuboriladi.",
    ]
    return "\n".join(lines)


async def process_reaction(
    chat_id: int,
    message_id: int,
    config: Config,
    analytics: AnalyticsReader,
    ombor: OmborBridgeClient,
    state: StateStore,
) -> ReactionResult:
    """
    Guruhdagi biror xabarga (istalgan reaksiya, istalgan kishidan) javoban
    chaqiriladi. "Jo'natildi" signali sifatida ishlaydi.
    """
    sync_key = _sync_key(chat_id, message_id)

    if sync_key in state.get_synced_keys():
        return ReactionResult(outcome="already_synced")

    if not ombor.is_configured:
        return ReactionResult(outcome="ombor_not_configured")

    order = await analytics.fetch_order_by_message(chat_id, message_id)
    if order is None:
        # Bu xabar buyurtma emas (oddiy suhbat) — Analytics'ning o'z parser'i
        # buni "orders"ga yozmagan. Hech narsa qilinmaydi.
        state.mark_not_an_order(sync_key)
        return ReactionResult(outcome="not_an_order")

    if not order.accepted:
        # Analytics hali qabul qilmagan (ko'rib chiqishda / rad etilgan). Ombor'ga
        # yuborilmaydi. Analytics'da tasdiqlangach - avtomatik qayta urinish
        # (har 10 daqiqa, 7 kungacha) o'zi yozadi. OWNER'ga xabar shart emas -
        # bu Analytics ko'rib chiqish navbatining ishi.
        state.mark_needs_review(sync_key, reason=f"Analytics'da hali qabul qilinmagan ({order.review_status})")
        return ReactionResult(outcome="needs_review", order_id=order.order_id, notify=False,
                              reason=f"Analytics'da hali qabul qilinmagan ({order.review_status})")

    if not order.items:
        return _needs_review(state, sync_key, "Buyurtmada hech qanday tovar qatori yo'q", order.order_id)

    # ERP: mahsulot xaritasi Ombor'da (Ombor #67, /product-mappings). Biz
    # Analytics kodlarini o'zicha yuboramiz - Ombor ularni mahsulotlarga
    # aylantiradi (tarkibli taom ham). Bog'lanmagan kod bo'lsa - Ombor 422
    # qaytaradi va hech narsa yozmaydi -> "ko'rib chiqish kerak".
    items = [{"code": item.product_code, "quantity": str(item.units_total)} for item in order.items]
    try:
        await ombor.push_sales_shipment_by_mapping(
            source_id=build_source_id("analytics-order", str(order.order_id)),
            system=ANALYTICS_SYSTEM,
            order_reference=str(order.order_id),
            items=items,
        )
        state.mark_synced(sync_key)
        return ReactionResult(outcome="synced", order_id=order.order_id)
    except BridgeError as e:
        missing = unmapped_codes(e)
        if missing:
            reason = f"Ombor'da bog'lanmagan kod: {', '.join(missing)}"
            return _needs_review(state, sync_key, reason, order.order_id)
        logger.warning("Order %s push failed: %s", order.order_id, e)
        state.mark_failed(sync_key, reason=str(e))
        return ReactionResult(outcome="failed", reason=str(e), order_id=order.order_id)


async def send_alert(ombor: OmborBridgeClient, key: str, level: str, message: str) -> bool:
    """erp-bridge-kit'ning umumiy usuli (v0.8.0+): Ombor /system-alerts ->
    OWNER'ga Telegram; hech qachon istisno ko'tarmaydi."""
    return await ombor.send_system_alert(source="sales-sync", key=key, level=level, message=message)


async def alert_for_result(ombor: OmborBridgeClient, chat_id: int, message_id: int, result: ReactionResult) -> None:
    """Yozilmagan sotuv - ⚠️; keyin muvaffaqiyatli yozilsa - ✅ (Ombor faqat
    oldin muammo yuborilgan bo'lsa jo'natadi)."""
    if result.order_id is None or result.outcome not in ("failed", "synced"):
        return
    key = f"sales-sync:order:{result.order_id}"
    link = message_link(chat_id, message_id) or f"xabar ID {message_id}"
    if result.outcome == "failed":
        await send_alert(ombor, key, "warning",
                         f"Buyurtma #{result.order_id} Ombor'ga yozilmadi (tayyor mahsulot kamaytirilmadi).\n"
                         f"Sabab: {(result.reason or '')[:600]}\nXabar: {link}\n"
                         "Tuzatilgach - xabardagi reaksiyani olib, qayta qo'ying.")
    else:
        await send_alert(ombor, key, "recovered", f"Buyurtma #{result.order_id} endi Ombor'ga yozildi.")
