"""Single-card home navigation."""
import logging
import time
from aiogram import Router,F
from aiogram.types import CallbackQuery,InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from access import check_callback
from keyboards import styled,list_picker
from database import get_lists
from telegram_retry import safe_call
router=Router(name='home_ui')
log=logging.getLogger('zhorik.perf03')
def home_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[styled('📋 Мои списки','home:lists'),styled('🍽 Меню на неделю','home:menu')],[styled('⚙️ Мои настройки','home:settings'),styled('💰 Бюджет и цены','home:prices')], [styled('💬 Разработчику','home:feedback'),styled('❓ Помощь','home:help')],[styled('❌ Закрыть','ui:close','danger')]])
@router.callback_query(F.data.startswith('home:'))
async def home(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    action=c.data.split(':')[1]
    started=time.perf_counter()
    await c.answer()
    await state.clear()
    if action=='root':
        await safe_call(c.message.delete)
    elif action=='help':
        await safe_call(c.message.edit_text,'❓ Помощь: /help\nСоздание списка — в разделе «Мои списки».',reply_markup=home_keyboard())
    elif action=='lists':
        rows=get_lists(c.from_user.id)
        await safe_call(c.message.edit_text,f'📋 Мои списки ({len(rows)})',reply_markup=list_picker(rows))
    elif action=='settings':
        from user_preferences import panel,keyboard
        await safe_call(c.message.edit_text,panel(c.from_user.id),reply_markup=keyboard())
    elif action=='prices':
        from price_ui import kb
        from price_storage import get_rows
        await safe_call(c.message.edit_text,f'💰 Бюджет и цены\nСохранено ценовых записей: {len(get_rows(c.from_user.id))}.',reply_markup=kb())
    elif action=='menu':
        from menu_ui import open_settings_from_inline
        await open_settings_from_inline(c.message,c.from_user.id)
    elif action=='feedback':
        from feedback_ui import kb
        await safe_call(c.message.edit_text,'💬 Написать разработчику — выберите тему.',reply_markup=kb())
    elif action=='new':
        from lists import ListInput
        await state.set_state(ListInput.name)
        await safe_call(c.message.edit_text,'Как назовём список? Отправьте текст или голос. Для отмены — /cancel.')
    elapsed=time.perf_counter()-started
    if elapsed >= 0.7:
        log.warning("PERF03 home:%s handling took %.3fs", action, elapsed)
