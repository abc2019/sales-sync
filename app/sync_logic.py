import logging
from dataclasses import dataclass

from erp_bridge_kit import BridgeError, OmborBridgeClient, build_source_id

from app.analytics_reader import AnalyticsReader
from app.config import Config
from app.state import StateStore

logger = logging.getLogger(__name__)


@dataclass
class ReactionResult:
    outcome: str  # "not_an_order" | "already_synced" | "needs_review" | "synced" | "failed" | "ombor_not_configured"
    reason: str | None = None


def _sync_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


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

    if not order.items:
        state.mark_needs_review(sync_key, reason="Buyurtmada hech qanday tovar qatori yo'q")
        return ReactionResult(outcome="needs_review", reason="Bo'sh buyurtma")

    resolved_items = []
    unresolved_codes = []
    for item in order.items:
        ombor_codes = config.product_code_map.get(item.product_code)
        if not ombor_codes:
            unresolved_codes.append(item.product_code)
            continue
        # Ba'zi taomlar (masalan "Qozon kabob") bitta buyurtma birligi
        # uchun bir nechta ALOHIDA bankaga (Ombor mahsuloti) bo'linadi —
        # har biriga BIR XIL miqdor (units_total) yuboriladi, chunki har
        # bir buyurtma birligi har bir komponentdan bittadan talab qiladi.
        for ombor_code in ombor_codes:
            resolved_items.append(
                {
                    "finished_product_external_code": ombor_code,
                    "quantity": str(item.units_total),
                    "unit": "dona",
                }
            )

    if unresolved_codes:
        reason = f"PRODUCT_CODE_MAP'da yo'q: {', '.join(sorted(set(unresolved_codes)))}"
        state.mark_needs_review(sync_key, reason=reason)
        return ReactionResult(outcome="needs_review", reason=reason)

    payload = {
        "source_id": build_source_id("analytics-order", str(order.order_id)),
        "order_reference": str(order.order_id),
        "items": resolved_items,
    }

    try:
        await ombor.push_sales_shipment(payload)
        state.mark_synced(sync_key)
        return ReactionResult(outcome="synced")
    except BridgeError as e:
        logger.warning("Order %s push failed: %s", order.order_id, e)
        state.mark_failed(sync_key, reason=str(e))
        return ReactionResult(outcome="failed", reason=str(e))
