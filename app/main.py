from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonCommands
from sqlalchemy import update

from app.config import Settings
from app.context import AppContext
from app.database import create_engine_and_session_factory, init_database
from app.handlers import admin, appearance, common, management, providers, store, wallet
from app.models import Product
from app.services.binance import BinancePayHistoryClient
from app.services.provider_catalog import provider_auto_sync_loop
from app.services.provider_registry import build_provider_registry
from app.services.settings import seed_runtime_settings
from app.services.ui_settings import load_ui_settings


def bot_commands(language: str = "es") -> list[BotCommand]:
    if language == "en":
        return [
            BotCommand(command="start", description="Open the store"),
            BotCommand(command="menu", description="Main menu"),
            BotCommand(command="tienda", description="Browse products"),
            BotCommand(command="wallet", description="Top up wallet"),
            BotCommand(command="historial", description="Purchases and deposits"),
            BotCommand(command="soporte", description="Contact support"),
            BotCommand(command="ajustes", description="Account settings"),
            BotCommand(command="cancel", description="Cancel the current operation"),
            BotCommand(command="admin", description="Administration panel"),
        ]
    return [
        BotCommand(command="start", description="Abrir la tienda"),
        BotCommand(command="menu", description="Menú principal"),
        BotCommand(command="tienda", description="Ver productos"),
        BotCommand(command="wallet", description="Recargar wallet"),
        BotCommand(command="historial", description="Compras y recargas"),
        BotCommand(command="soporte", description="Contactar soporte"),
        BotCommand(command="ajustes", description="Ajustes de la cuenta"),
        BotCommand(command="cancel", description="Cancelar la operación actual"),
        BotCommand(command="admin", description="Panel de administración"),
    ]


async def configure_bot(bot: Bot) -> None:
    private_scope = BotCommandScopeAllPrivateChats()
    await bot.set_my_commands(bot_commands("es"), scope=private_scope)
    await bot.set_my_commands(bot_commands("en"), scope=private_scope, language_code="en")
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


async def run() -> None:
    config = Settings()
    config.ensure_local_directories()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    logger = logging.getLogger(__name__)

    engine, session_factory = create_engine_and_session_factory(config.database_url)
    await init_database(engine)
    async with session_factory() as session:
        await seed_runtime_settings(session, config)
        await load_ui_settings(session)

    binance: BinancePayHistoryClient | None = None
    if config.binance_verification_enabled:
        assert config.binance_api_key is not None
        assert config.binance_api_secret is not None
        binance = BinancePayHistoryClient(
            api_key=config.binance_api_key.get_secret_value(),
            api_secret=config.binance_api_secret.get_secret_value(),
            base_url=config.binance_base_url,
            history_hours=config.binance_history_hours,
            cache_seconds=config.binance_cache_seconds,
            recv_window_ms=config.binance_recv_window_ms,
            request_timeout_seconds=config.binance_request_timeout_seconds,
        )
        logger.info("Binance Pay automatic verification is enabled")
    else:
        logger.warning("Binance API credentials are missing; deposits will require manual approval")

    providers_registry = build_provider_registry(config)
    active_provider_codes = [runtime.config.code for runtime in providers_registry.values()]
    async with session_factory() as session:
        statement = update(Product).where(Product.provider_code.is_not(None))
        if active_provider_codes:
            statement = statement.where(Product.provider_code.not_in(active_provider_codes))
        await session.execute(statement.values(provider_in_stock=False, provider_stock=0))
        await session.commit()
    if len(providers_registry):
        logger.info("External API providers enabled: %s", len(providers_registry))
    else:
        logger.info("No external API providers are configured")

    bot = Bot(
        token=config.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(
        storage=MemoryStorage(),
        events_isolation=SimpleEventIsolation(),
    )
    ctx = AppContext(
        config=config,
        session_factory=session_factory,
        binance=binance,
        providers=providers_registry,
    )
    dispatcher["ctx"] = ctx

    dispatcher.include_router(common.router)
    dispatcher.include_router(admin.router)
    dispatcher.include_router(appearance.router)
    dispatcher.include_router(management.router)
    dispatcher.include_router(providers.router)
    dispatcher.include_router(wallet.router)
    dispatcher.include_router(store.router)

    for runtime in providers_registry.values():
        if runtime.config.auto_sync_minutes > 0:
            ctx.spawn(
                provider_auto_sync_loop(
                    session_factory,
                    runtime,
                    bot=bot,
                    admin_ids=config.admin_ids,
                )
            )

    try:
        me = await bot.get_me()
        if config.bot_id is not None and me.id != config.bot_id:
            raise RuntimeError(
                f"BOT_ID mismatch: configured {config.bot_id}, token belongs to {me.id}"
            )
        await configure_bot(bot)
        await bot.delete_webhook(drop_pending_updates=config.drop_pending_updates)
        logger.info("Starting @%s (%s)", me.username, me.id)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await ctx.shutdown_background_tasks()
        if binance is not None:
            await binance.close()
        await providers_registry.close()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        logging.exception("Fatal bot error")
        sys.exit(1)


if __name__ == "__main__":
    main()
