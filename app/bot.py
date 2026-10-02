"""
Yupqa Telegram qatlami — faqat sozlangan guruhdagi xabarlarga reaksiya
qo'shilganini kuzatadi va app.sync_logic.process_reaction()ni chaqiradi.
Barcha biznes mantiq sync_logic.py'da, tarmoqsiz test qilinadi.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import MessageReactionUpdated

from erp_bridge_kit import ModuleClient, OmborBridgeClient

from app.analytics_reader import PostgresAnalyticsReader
from app.config import Config, load_config
from app.state import StateStore
from app.sync_logic import alert_for_result, build_review_message, process_reaction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_dispatcher(
    config: Config, analytics: PostgresAnalyticsReader, ombor: OmborBridgeClient, state: StateStore
) -> Dispatcher:
    dp = Dispatcher()

    @dp.message_reaction()
    async def on_reaction(event: MessageReactionUpdated, bot: Bot) -> None:
        if event.chat.id != config.sales_group_chat_id:
            return
        if not event.new_reaction:
            return  # reaksiya olib tashlandi, qo'shilmadi

        result = await process_reaction(
            chat_id=event.chat.id,
            message_id=event.message_id,
            config=config,
            analytics=analytics,
            ombor=ombor,
            state=state,
        )
        logger.info(
            "Reaction on %s:%s -> %s (%s)",
            event.chat.id, event.message_id, result.outcome, result.reason,
        )
        await alert_for_result(ombor, event.chat.id, event.message_id, result)
        if result.outcome == "needs_review" and result.notify:
            await notify_review(bot, config, event.chat.id, event.message_id, result)

    return dp


async def notify_review(bot, config: Config, chat_id: int, message_id: int, result) -> int:
    """'Ko'rib chiqish kerak' buyurtma haqida OWNER'larga xabar. Yuborib
    bo'lmasa (masalan OWNER botga /start bosmagan) - log, sinxronizatsiya
    to'xtamaydi. Qaytaradi: yetkazilgan xabarlar soni."""
    if not config.review_notify_chat_ids:
        logger.warning(
            "needs_review %s:%s - REVIEW_NOTIFY_CHAT_IDS sozlanmagan, OWNER'ga xabar yuborilmadi",
            chat_id, message_id,
        )
        return 0
    text = build_review_message(chat_id, message_id, result)
    delivered = 0
    for target in config.review_notify_chat_ids:
        try:
            await bot.send_message(target, text, disable_web_page_preview=True)
            delivered += 1
        except Exception:  # noqa: BLE001
            logger.exception("needs_review xabarini %s ga yuborib bo'lmadi (botga /start bosilganmi?)", target)
    return delivered


def build_ombor_client(config: Config) -> OmborBridgeClient:
    return OmborBridgeClient(
        ModuleClient(
            config.ombor_api_base_url,
            actor_name=config.ombor_actor_name,
            api_token=config.ombor_api_token,
        )
    )


async def main() -> None:
    config = load_config()
    state = StateStore(config.state_database_path)
    analytics = PostgresAnalyticsReader(config.analytics_database_url)
    ombor = build_ombor_client(config)
    if config.ombor_api_token is None:
        logger.warning(
            "OMBOR_API_TOKEN sozlanmagan - Ombor'ga tokensiz (eski header) kiriladi; "
            "Ombor enforce rejimiga o'tganda rad etiladi."
        )

    bot = Bot(token=config.sales_bot_token)
    dp = build_dispatcher(config, analytics, ombor, state)

    if not config.review_notify_chat_ids:
        logger.warning("REVIEW_NOTIFY_CHAT_IDS sozlanmagan - 'ko'rib chiqish kerak' buyurtmalar faqat logda qoladi")
    if config.product_code_map:
        logger.warning(
            "PRODUCT_CODE_MAP endi ishlatilmaydi: mahsulot xaritasi Ombor'da (ERP ma'lumotnomasi). "
            "Uni bir marta Ombor botiga yuboring (⚙️ Sozlamalar → 🔗 Mahsulot kodlari) va o'zgaruvchini o'chiring."
        )
    logger.info("sales-sync boshlandi (guruh=%s)", config.sales_group_chat_id)
    try:
        await dp.start_polling(bot, allowed_updates=["message_reaction"])
    finally:
        state.close()


if __name__ == "__main__":
    asyncio.run(main())
