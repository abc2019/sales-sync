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

    @dp.message_reaction()
    async def on_reaction(event: MessageReactionUpdated) -> None:
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

    return dp


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

    logger.info("sales-sync boshlandi (guruh=%s)", config.sales_group_chat_id)
    try:
        await dp.start_polling(bot, allowed_updates=["message_reaction"])
    finally:
        state.close()


if __name__ == "__main__":
    asyncio.run(main())
