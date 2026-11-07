from aiogram import Router, F
from telegram_retry import safe_call
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from access import check_message
from keyboards import MAIN_MENU, main_menu_for
from nutrition_ui import keyboard as nutrition_keyboard
import texts

router = Router(name="stubs")


@router.message(F.text == "__unused_menu_stub__")
async def menu_stub(message: Message, state: FSMContext):
    if not await check_message(message):
        return
    await state.clear()
    await safe_call(message.answer, texts.MENU_NOT_READY)


@router.message(F.text == "__unused_settings_stub__")
async def settings_stub(message: Message, state: FSMContext):
    if not await check_message(message):
        return
    await state.clear()
    await safe_call(message.answer, texts.SETTINGS_NOT_READY + "\n\n" + texts.DISCLAIMER, reply_markup=main_menu_for(message.from_user.id))


@router.message(F.text == "❓ Помощь")
async def help_message(message: Message, state: FSMContext):
    if not await check_message(message):
        return
    await state.clear()
    await safe_call(message.answer, texts.HELP + "\n\n" + texts.DISCLAIMER, reply_markup=nutrition_keyboard())


@router.message()
async def fallback(message: Message):
    if not await check_message(message):
        return
    await safe_call(message.answer, texts.FALLBACK, reply_markup=main_menu_for(message.from_user.id))
