"""Pagination and item card callbacks."""
from aiogram import Router,F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup,State
from aiogram.types import CallbackQuery,Message
from access import check_callback,check_message
from keyboards import paged_list_keyboard,item_keyboard
from list_storage import get_page,get_item,update_item,delete_item,change_selection,set_selection_all
from telegram_retry import safe_call
router=Router(name='list_ui')
class ItemEdit(StatesGroup):
    text=State()
def page_text(d):
    start=d['page']*5+1 if d['count'] else 0
    end=min(d['count'],(d['page']+1)*5)
    return f"📋 {d['title']}\nПункты: {start}–{end} / {d['count']}\n\n"+("\n".join(f"{'✅' if x['selected'] else '⬜'} {d['page']*5+i}. {x['text']} — {x['quantity']:g} {x['unit']}" for i,x in enumerate(d['items'],1)) if d['items'] else 'Пока пусто. Нажмите «➕ Добавить».')
async def show(callback,list_id,page=0):
    d=get_page(callback.from_user.id,list_id,page)
    if not d:
        await callback.answer('Список больше не существует',show_alert=True);return
    await safe_call(callback.message.edit_text,page_text(d),reply_markup=paged_list_keyboard(list_id,d))
@router.callback_query(F.data.startswith('list:page:'))
async def page(callback:CallbackQuery,state:FSMContext):
    if not await check_callback(callback):return
    _,_,lid,n=callback.data.split(':');await state.clear();await show(callback,int(lid),int(n));await callback.answer()
@router.callback_query(F.data.startswith('item:'))
async def item_action(callback:CallbackQuery,state:FSMContext):
    if not await check_callback(callback):return
    parts=callback.data.split(':')
    if len(parts)!=5:return await callback.answer('Устаревшая кнопка',show_alert=True)
    _,action,lid,iid,page=parts;lid=int(lid);iid=int(iid);page=int(page)
    item=get_item(callback.from_user.id,lid,iid)
    if not item:return await callback.answer('Пункт больше не существует',show_alert=True)
    if action in ('toggle','plus','minus'):
        change_selection(callback.from_user.id,lid,iid,action)
        await show(callback,lid,page)
    elif action=='open':
        await safe_call(callback.message.edit_text,f"✏️ Пункт: {item['text']}",reply_markup=item_keyboard(lid,iid,page))
    elif action=='edit':
        await state.update_data(edit_list_id=lid,edit_item_id=iid,edit_page=page)
        await state.set_state(ItemEdit.text)
        await safe_call(callback.message.answer,f"Отправьте новое название и количество для «{item['text']}» текстом.")
    elif action=='delete':
        from aiogram.types import InlineKeyboardMarkup
        from keyboards import styled
        kb=InlineKeyboardMarkup(inline_keyboard=[[styled('🗑 Да, удалить',f'item:confirm:{lid}:{iid}:{page}','danger')],[styled('❌ Отмена',f'item:open:{lid}:{iid}:{page}','danger')]])
        await safe_call(callback.message.edit_text,f"Удалить пункт «{item['text']}»?",reply_markup=kb)
    elif action=='confirm':
        delete_item(callback.from_user.id,lid,iid);await show(callback,lid,page)
    else:return await callback.answer('Устаревшая кнопка',show_alert=True)
    await callback.answer()
@router.message(ItemEdit.text,F.text)
async def save_edit(message:Message,state:FSMContext):
    if not await check_message(message):return
    d=await state.get_data();text=message.text.strip()
    if not 1<=len(text)<=500:return await safe_call(message.answer,'Введите от 1 до 500 символов.')
    ok=update_item(message.from_user.id,d['edit_list_id'],d['edit_item_id'],text)
    page=get_page(message.from_user.id,d['edit_list_id'],d['edit_page'])
    await state.clear()
    await safe_call(message.answer,('✅ Изменено.\n\n'+page_text(page)) if ok and page else 'Пункт не найден.',reply_markup=paged_list_keyboard(d['edit_list_id'],page) if page else None)
@router.callback_query(F.data=='ui:close')
async def close(callback:CallbackQuery,state:FSMContext):
    if not await check_callback(callback):return
    # Close only this bot card. A service notification must not silently
    # discard an unrelated active wizard or pending user input.
    active_input=await state.get_state()
    from common import WELCOME_PAIRS
    greeting_id=WELCOME_PAIRS.pop((callback.message.chat.id,callback.message.message_id),None)
    try:
        await safe_call(callback.message.delete)
    except Exception:
        pass  # Telegram may already have removed this message.
    if greeting_id:
        # Reply keyboard was attached to the greeting; restore it after deletion.
        try:
            from keyboards import MAIN_MENU, main_menu_for
            await safe_call(callback.bot.send_message, callback.message.chat.id,
                            '🏠 Главное меню Жорика', reply_markup=main_menu_for(callback.from_user.id))
        except Exception:
            pass
        try:
            await safe_call(callback.bot.delete_message,callback.message.chat.id,greeting_id)
        except Exception:
            pass  # The greeting may be older than Telegram's deletion window.
    try:
        if active_input:
            await callback.answer('Ввод продолжается; /cancel — отменить.')
        else:
            await callback.answer()
    except Exception:
        pass

@router.callback_query(F.data.startswith(('list:more:','list:exports:')))
async def submenus(c:CallbackQuery):
    if not await check_callback(c):return
    from keyboards import list_more_keyboard,list_export_keyboard
    from database import get_list
    _,action,raw=c.data.split(':')
    if not raw.isdigit() or not get_list(c.from_user.id,int(raw)):
        return await c.answer('Список не найден',show_alert=True)
    lid=int(raw)
    markup=list_more_keyboard(lid) if action=='more' else list_export_keyboard(lid)
    await safe_call(c.message.edit_text,'⚙️ Дополнительные действия' if action=='more' else '📤 Экспорт списка',reply_markup=markup)
    await c.answer()

@router.callback_query(F.data.startswith('list:overview:'))
async def overview(callback:CallbackQuery,state:FSMContext):
    if not await check_callback(callback):return
    from database import get_lists
    from keyboards import list_picker
    page=max(0,int(callback.data.rsplit(':',1)[1]))
    rows=get_lists(callback.from_user.id)
    page=min(page,max(0,(len(rows)-1)//5))
    await state.clear()
    await safe_call(callback.message.edit_text,f'📋 Мои списки ({len(rows)})',reply_markup=list_picker(rows,page=page))
    await callback.answer()

@router.callback_query(F.data.startswith(('list:selectall:','list:clearall:')))
async def bulk_selection(c:CallbackQuery):
    if not await check_callback(c):return
    _,action,lid,page=c.data.split(':')
    if not set_selection_all(c.from_user.id,int(lid),action=='selectall'):
        return await c.answer('Список не найден',show_alert=True)
    await show(c,int(lid),int(page));await c.answer()
