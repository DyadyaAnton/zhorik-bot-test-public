from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from access import check_message
from telegram_retry import safe_call
from price_catalog import price_coverage, RETAILERS

router = Router(name="menu")

@router.message(Command("prices"))
async def prices(message: Message):
    if not await check_message(message): return
    products, observations = price_coverage()
    await safe_call(message.answer,
        f"🧺 Справочник продуктов: {products} позиций.\n"
        f"Подтверждённых ценовых наблюдений: {observations}.\n\n"
        "Сети: " + ", ".join(RETAILERS) + ".\n\n"
        "Цены не подменяются вымышленными. Для расчёта бюджета нужны проверенные цены с датой, упаковкой и источником."
    )

