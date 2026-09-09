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
from app.sync_logic import process_reaction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def build_dispatcher(
    config: Config, analytics: PostgresAnalyticsReader, ombor: OmborBridgeClient, state: StateStore
) -> Dispatcher:
    dp = Dispatcher()

    @dp.message()
    async def on_any_message(message) -> None:
        # Faqat diagnostika uchun — polling umuman ishlayaptimi, shuni ko'rsatadi.
        logger.info("RAW MESSAGE: chat.id=%s text=%r", message.chat.id, message.text)

    @dp.message_reaction()
    async def on_reaction(event: MessageReactionUpdated) -> None:
        logger.info(
            "RAW REACTION: chat.id=%s (kutilgan=%s) message_id=%s new=%s old=%s",
            event.chat.id, config.sales_group_chat_id, event.message_id,
            event.new_reaction, event.old_reaction,
        )
        if event.chat.id != config.sales_group_chat_id:
            logger.warning(
                "Chat ID mos kelmadi: kelgan=%s, kutilgan=%s — o'tkazib yuborildi",
                event.chat.id, config.sales_group_chat_id,
            )
            return
        if not event.new_reaction:
            logger.info("Reaksiya olib tashlandi (qo'shilmadi) — o'tkazib yuborildi")
            return

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

    return dp


async def main() -> None:
    config = load_config()
    state = StateStore(config.state_database_path)
    analytics = PostgresAnalyticsReader(config.analytics_database_url)
    ombor = OmborBridgeClient(
        ModuleClient(config.ombor_api_base_url, actor_name=config.ombor_actor_name)
    )

    bot = Bot(token=config.sales_bot_token)
    dp = build_dispatcher(config, analytics, ombor, state)

    logger.info("sales-sync boshlandi (guruh=%s)", config.sales_group_chat_id)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "message_reaction"])
    finally:
        state.close()


if __name__ == "__main__":
    asyncio.run(main())
