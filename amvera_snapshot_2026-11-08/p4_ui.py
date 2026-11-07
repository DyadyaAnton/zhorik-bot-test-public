"""Owner-only read-only diagnostics for P4B, available directly in Telegram."""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from keyboards import styled
from access import check_message, check_callback
from config import settings
from database import connect_db
from p4_bridge import estimate_saved_menu, format_diagnostic

router = Router(name='p4_ui')

def _close_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[styled('✖️ Закрыть', 'p4:close', 'danger')]])

@router.callback_query(F.data == 'p4:close')
async def close_diagnostic(callback: CallbackQuery):
    if not callback.from_user or callback.from_user.id != settings.sandbox_owner_id:
        return await callback.answer('Недоступно', show_alert=True)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass  # Message may already be deleted or no longer deletable.


def _latest_menu_id(db, uid):
    row = db.execute('SELECT id FROM menus WHERE owner_id=? ORDER BY id DESC LIMIT 1', (uid,)).fetchone()
    return row['id'] if row else None


@router.message(F.text.regexp(r'^/p4_check(?:@\w+)?(?:\s+\d+)?\s*$'))
async def check_p4(message: Message):
    if not await check_message(message):
        return
    uid = message.from_user.id
    if uid != settings.sandbox_owner_id:
        return await message.answer('Диагностика доступна только владельцу.')
    parts = message.text.split()
    with connect_db() as db:
        menu_id = int(parts[1]) if len(parts) > 1 else _latest_menu_id(db, uid)
        result = estimate_saved_menu(db, uid, menu_id) if menu_id is not None else None
    await message.answer(format_diagnostic(result), reply_markup=_close_kb())


@router.callback_query(F.data.startswith('p4:estimate:'))
async def estimate_callback(callback: CallbackQuery):
    if not await check_callback(callback):
        return
    uid = callback.from_user.id
    try:
        menu_id = int(callback.data.rsplit(':', 1)[-1])
    except ValueError:
        return await callback.answer('Неверный номер меню', show_alert=True)
    with connect_db() as db:
        result = estimate_saved_menu(db, uid, menu_id)
    await callback.answer()
    await callback.message.answer(format_diagnostic(result), reply_markup=_close_kb())
