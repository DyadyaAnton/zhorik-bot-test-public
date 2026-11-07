import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from database import init_db, connect_db
from receipt_admin import router as receipt_admin_router
from common import router as common_router
from lists import router as lists_router
from voice import router as voice_router
from stubs import router as stubs_router
from admin import router as admin_router
from menu_ui import router as menu_ui_router
from price_catalog import seed_catalog
from list_ui import router as list_ui_router
from shop_settings import router as shop_router,init_settings
from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from price_storage import init as init_user_prices
from price_ui import router as price_ui_router
from list_editor import router as list_editor_router
from home_ui import router as home_ui_router
from reference_prices import init as init_reference_prices
from list_backup import router as list_backup_router
from admin_prices import router as admin_prices_router
from user_preferences import router as preferences_router
from feedback_ui import router as feedback_router,init as init_feedback
from nutrition_library import init as init_nutrition
from nutrition_ui import router as nutrition_router
from lenta_public_ui16 import router as lenta_public_ui16_router
from catalog_bridge_ui16 import router as catalog_bridge_ui16_router
from price07_rev2_core import init as init_price07_rev2
from price07_rev2_runner import weekly_loop as price07_rev2_weekly_loop, auto_refresh_enabled
from price07_ui import router as price07_ui_router
from menu import router as menu_router
from vkusvill_price_ui import router as vkusvill_price_router
from vkusvill_approval import router as vkusvill_approval_router
from vkusvill_test_lab import router as vkusvill_test_lab_router
from p4_ui import router as p4_ui_router
from store_catalog_lab import router as store_catalog_lab_router
from price_plan_ui import router as price_plan_router
from price02_ui import router as price02_router
from recipe_ui import router as recipe_router
from recipe_bank import init as init_recipe_bank
from recipe_editorial import init as init_editorial_recipes
from menu_diet import init as init_menu_diet
from menu_budget import init as init_menu_budget
from card_registry import init as init_cards, RegistryMiddleware

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("zhorik")


async def global_error_handler(event: ErrorEvent) -> bool:
    error = event.exception
    log.exception("Необработанная ошибка Telegram update", exc_info=error)
    # Не завершаем polling из-за ошибки одного update.
    return True


async def main() -> None:
    settings.validate()
    from full_db_backup import create_full_backup
    if settings.db_path.exists():
        create_full_backup()
    init_db()
    from llm_usage import recover_unsettled_on_startup
    recovered_llm_reservations = recover_unsettled_on_startup()
    if recovered_llm_reservations:
        logging.getLogger(__name__).warning('Reconciled %d interrupted LLM reservations', recovered_llm_reservations)
    from p4_approval_core import migrate_legacy
    with connect_db() as db:
        migrate_legacy(db)
    seed_catalog()
    init_settings()
    init_user_prices()
    init_reference_prices()
    init_feedback()
    init_nutrition()
    init_menu_diet()
    init_menu_budget()
    from menu_mode import init as init_menu_mode
    init_menu_mode()
    from menu_library import init as init_menu_library
    init_menu_library()
    from porridge_builder import init as init_porridge_builder
    init_porridge_builder()
    from dish_builder import init as init_dish_builder
    init_dish_builder()
    init_cards()
    # create_full_backup() runs before all database schema initialization.
    with connect_db() as db:
        init_price07_rev2(db)
    with connect_db() as db:
        init_recipe_bank(db)
        init_editorial_recipes(db)

    bot = Bot(token=settings.telegram_bot_token)
    bot.session.middleware(RegistryMiddleware())
    dp = Dispatcher(storage=MemoryStorage())
    from perf_diagnostics import UpdateTimingMiddleware
    dp.message.outer_middleware(UpdateTimingMiddleware())
    dp.callback_query.outer_middleware(UpdateTimingMiddleware())
    dp.include_router(common_router)
    dp.include_router(nutrition_router)
    dp.include_router(lenta_public_ui16_router)
    dp.include_router(catalog_bridge_ui16_router)
    dp.include_router(price07_ui_router)
    dp.include_router(menu_router)
    dp.include_router(p4_ui_router)
    dp.include_router(store_catalog_lab_router)
    dp.include_router(price_plan_router)
    dp.include_router(price02_router)
    dp.include_router(recipe_router)
    dp.include_router(vkusvill_test_lab_router)
    dp.include_router(vkusvill_price_router)
    dp.include_router(vkusvill_approval_router)
    dp.include_router(lists_router)
    dp.include_router(list_ui_router)
    dp.include_router(list_editor_router)
    dp.include_router(list_backup_router)
    dp.include_router(home_ui_router)
    dp.include_router(preferences_router)
    dp.include_router(feedback_router)
    dp.include_router(admin_prices_router)
    dp.include_router(price_ui_router)
    dp.include_router(shop_router)
    dp.include_router(voice_router)
    dp.include_router(menu_ui_router)
    dp.include_router(admin_router)
    dp.include_router(receipt_admin_router)
    dp.include_router(stubs_router)
    dp.errors.register(global_error_handler)

    # /begin remains a supported alias of /start in common.py, but showing
    # both in Telegram's blue command menu is redundant. Keep the handler for
    # old messages and deep links; expose one discoverable entry point.
    commands = [BotCommand(command="start", description="Главное меню"),
                BotCommand(command="help", description="Помощь"),
                BotCommand(command="calories", description="Таблица КБЖУ"),
                BotCommand(command="clean", description="Закрыть диалоги"),
                BotCommand(command="lists", description="Мои списки"),
                BotCommand(command="new", description="Новый список"),
                BotCommand(command="prices", description="Справочник цен"),
                BotCommand(command="recipes", description="Рецепты по рубрикам"),
                BotCommand(command="myprices", description="Единый каталог цен"),
                BotCommand(command="catalog_prices", description="Каталог товаров и цен"),
                BotCommand(command="settings", description="Мои настройки"),
                BotCommand(command="preferences", description="Мои настройки"),
                BotCommand(command="feedback", description="Написать разработчику"),
                BotCommand(command="cancel", description="Отмена")]
    # Frequent commands first; every other command follows alphabetical order.
    command_order = lambda item: (0 if item.command == 'clean' else
                                  1 if item.command == 'start' else
                                  2 if item.command == 'version' else 3,
                                  item.command.casefold())
    commands.sort(key=command_order)
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    except Exception as e:
        log.warning("Не удалось обновить команды Telegram: %s",e)
    owner_commands = commands + [BotCommand(command="version", description="Версия Жорика"), BotCommand(command="recipes_export", description="Скачать рецепты JSON"), BotCommand(command="recipes_import", description="Загрузить рецепты JSON"), BotCommand(command="prices_plan", description="Прайс-ориентир 258 продуктов"), BotCommand(command="p4_check", description="Поставка 4: проверить смету"), BotCommand(command="admin", description="Админ-панель"), BotCommand(command="perf", description="Диагностика скорости"),
        BotCommand(command="catalog_links", description="Диагностика связей каталогов"),
        BotCommand(command="lenta_stores_probe", description="Лента: московские магазины"),
        BotCommand(command="lenta_price_probe", description="Лента: проверка предложения"),
        BotCommand(command="price07_status", description="PRICE07: недельное обновление цен"),
        BotCommand(command="price07_refresh", description="PRICE07: запустить обновление"),
        BotCommand(command="price07_all_status", description="PRICE07 REV2: источники и прогресс"),
        BotCommand(command="price07_all_refresh", description="PRICE07 REV2: актуализировать цены"),
        BotCommand(command="price07_sku_check", description="PRICE07: проверить SKU (без записи)"),
        BotCommand(command="price07_sku_bind", description="PRICE07: ручная привязка SKU"),
        BotCommand(command="price07_nutrition_verify", description="PRICE07: проверить КБЖУ нового товара"),
        BotCommand(command="admin_users", description="Белый список"),
        BotCommand(command="admin_allow", description="Разрешить доступ по ID"),
        BotCommand(command="admin_deny", description="Отозвать доступ по ID"),
        BotCommand(command="admin_update_prices", description="Актуализировать цены"),
        BotCommand(command="admin_prices", description="Управление базой цен"),
        BotCommand(command="price02_probe", description="PRICE 02: проверить цены магазинов"),
        BotCommand(command="price03_probe", description="PRICE 03: Лента и Ашан — кандидаты цен"),
        BotCommand(command="price03_import", description="PRICE 03: перенос исторических цен"),
        BotCommand(command="price06_collect", description="PRICE 06: собрать следующую порцию цен"),
        BotCommand(command="vkusvill_prices", description="Проверить цены ВкусВилла"),
        BotCommand(command="vkusvill_test", description="Лаборатория ВкусВилла"),
        BotCommand(command="store_lab", description="Лаборатория Магнит/Пятёрочка"),
        BotCommand(command="magnit_prices", description="Проверить страницу Магнита"),
        BotCommand(command="pyaterochka_prices", description="Проверить страницу Пятёрочки"),
        BotCommand(command="store_batch", description="Пакетная проверка магазинов"),
        BotCommand(command="admin_feedback", description="Обращения пользователей")]
    owner_commands.sort(key=command_order)
    try:
        await bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=settings.sandbox_owner_id))
    except Exception as e:
        log.warning("Не удалось обновить команды владельца: %s",e)
    log.info("Жорик запускается в закрытом режиме. OWNER_ID задан.")
    price07_task = (asyncio.create_task(price07_rev2_weekly_loop(), name='price07_rev2_weekly')
                    if auto_refresh_enabled() else None)
    try:
        await dp.start_polling(bot)
    finally:
        if price07_task is not None:
            price07_task.cancel()
            try:
                await price07_task
            except asyncio.CancelledError:
                pass
        from menu_ui import wait_for_pending_saves
        from menu_workers import shutdown_pool
        try:
            await wait_for_pending_saves(timeout=20)
        finally:
            shutdown_pool()
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        log.exception("Жорик завершился из-за ошибки")
        raise
