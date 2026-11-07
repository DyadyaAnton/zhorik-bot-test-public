from keyboards import styled
import secrets
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from access import check_message, check_callback
from database import create_list, get_lists, get_list, add_items, render_list, delete_list, rename_list, list_item_count
from keyboards import MAIN_MENU, main_menu_for, list_picker, list_actions_keyboard, confirm_keyboard
from telegram_retry import safe_call
from texts import STALE_BUTTON
from product_parser import normalize_title
from phrase_parser import parse_phrase
from list_storage import get_page,selected_export
from list_ui import page_text
from keyboards import title_preview,paged_list_keyboard

router = Router(name="lists")

class ListInput(StatesGroup):
    name = State()
    rename = State()
    items = State()
    edit_draft = State()


def split_items(raw):
    import re
    raw=(raw or '').strip()
    if not raw:return []
    # Explicitly separated items are literal user data, not a food dictionary.
    if re.search(r'[,;\n]',raw):
        chunks=re.split(r'[,;\n]+',raw)
        return [re.sub(r'^\s*(?:[•\-]|\d+[.)])\s*','',p).strip()[:500] for p in chunks if p.strip()]
    parsed=parse_phrase(raw,'shopping')
    if parsed.uncertain:
        # Single unknown phrase is preserved; mixed unseparated speech needs clarification.
        if not parsed.items:
            value=re.sub(r'^(?:купи(?:те)?|купить|надо купить|нужно купить|добавь(?:те)? в список покупок)\s+','',raw,flags=re.I).strip()
            return [value[:500]] if value else []
        return []
    return list(parsed.items)

async def show_lists(message, user_id):
    rows = get_lists(user_id)
    await safe_call(message.answer, f"📋 Мои списки ({len(rows)}):\nВыберите список или создайте новый.", reply_markup=list_picker(rows))

@router.message(Command("lists"))
@router.message(F.text == "📋 Мои списки")
async def my_lists(message: Message, state: FSMContext):
    if not await check_message(message): return
    await state.clear()
    await show_lists(message, message.from_user.id)

@router.message(Command("new"))
@router.message(F.text == "➕ Новый список")
async def new_list(message: Message, state: FSMContext):
    if not await check_message(message): return
    await state.clear()
    await state.set_state(ListInput.name)
    await safe_call(message.answer, "Как назовём список? Отправьте название текстом или голосом. Например: «На дачу». Для отмены — /cancel.")

@router.message(ListInput.name, F.text)
async def list_name(message: Message, state: FSMContext):
    if not await check_message(message): return
    title = (message.text or "").strip()
    if not 1 <= len(normalize_title(title)) <= 100:
        await safe_call(message.answer, "Название должно быть от 1 до 100 символов.")
        return
    token=secrets.token_hex(8)
    await state.update_data(pending_title=normalize_title(title),pending_title_token=token)
    await state.set_state(None)
    await safe_call(message.answer, f"📋 Создать список «{normalize_title(title)}»?",reply_markup=title_preview(token))

@router.callback_query(F.data == "list:back")
async def back(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    await state.clear()
    from keyboards import list_picker
    rows=get_lists(callback.from_user.id)
    await safe_call(callback.message.edit_text,f"📋 Мои списки ({len(rows)})",reply_markup=list_picker(rows))
    await callback.answer()

@router.callback_query(F.data.startswith("list:open:"))
async def open_list(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    list_id = int(callback.data.rsplit(":", 1)[1])
    body = render_list(callback.from_user.id, list_id)
    if body is None:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    data=get_page(callback.from_user.id,list_id)
    await safe_call(callback.message.edit_text, page_text(data), reply_markup=paged_list_keyboard(list_id,data))
    await callback.answer()

@router.callback_query(F.data.startswith("list:add:"))
async def start_add(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    list_id = int(callback.data.rsplit(":", 1)[1])
    if not get_list(callback.from_user.id, list_id):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    await state.update_data(target_list_id=list_id)
    await state.set_state(ListInput.items)
    await safe_call(callback.message.answer, "Назовите продукты с количествами обычной фразой, например: «мясо два килограмма молоко три бутылки уголь». Паузы не обязательны. Или отправьте пункты текстом, по одному в строке. Перед сохранением покажу результат.")
    await callback.answer()

async def preview_items(message, state, raw):
    items = split_items(raw)
    if not items:
        await safe_call(message.answer, "Не удалось уверенно разделить все продукты. Уточните названия, поставьте запятые или отправьте по одному в строке.")
        return
    if len(items) > 100:
        await safe_call(message.answer, "За один раз можно добавить не более 100 пунктов.")
        return
    token = secrets.token_hex(8)
    await state.update_data(draft_items=items, draft_token=token)
    await state.set_state(None)
    body = "\n".join(f"{i}. {s}" for i,s in enumerate(items,1))
    await safe_call(message.answer, "Проверим перед сохранением:\n\n"+body, reply_markup=confirm_keyboard(token))

@router.message(ListInput.items, F.text)
async def items_text(message: Message, state: FSMContext):
    if not await check_message(message): return
    await preview_items(message, state, message.text)

@router.callback_query(F.data.startswith("draft:"))
async def draft_action(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    parts = callback.data.split(":")
    data = await state.get_data()
    if len(parts) != 3 or parts[2] != data.get("draft_token"):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    action = parts[1]
    if action == "edit":
        await state.update_data(draft_token=None)
        await state.set_state(ListInput.edit_draft)
        await safe_call(callback.message.answer, "Отправьте исправленный список пунктов текстом.")
    elif action == "close":
        await state.clear()
        await safe_call(callback.message.delete)
    elif action == "cancel":
        list_id = data.get("target_list_id")
        await state.clear()
        await safe_call(callback.message.answer, "Добавление отменено.", reply_markup=list_actions_keyboard(list_id))
    elif action == "save":
        list_id = data.get("target_list_id")
        items = data.get("draft_items") or []
        if not list_id or not get_list(callback.from_user.id, list_id):
            await callback.answer(STALE_BUTTON, show_alert=True)
            return
        count = add_items(callback.from_user.id, list_id, "\n".join(items))
        await state.clear()
        from list_storage import get_page
        d=get_page(callback.from_user.id,list_id)
        await safe_call(callback.message.edit_text, f"✅ Добавлено пунктов: {count}.\n\n"+page_text(d), reply_markup=paged_list_keyboard(list_id,d))
    else:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    await callback.answer()

@router.message(ListInput.edit_draft, F.text)
async def edit_draft(message: Message, state: FSMContext):
    if not await check_message(message): return
    await preview_items(message, state, message.text)

@router.callback_query(F.data.startswith("list:rename:"))
async def ask_rename(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    list_id = int(callback.data.rsplit(":", 1)[1])
    if not get_list(callback.from_user.id, list_id):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    await state.clear()
    await state.update_data(target_list_id=list_id)
    await state.set_state(ListInput.rename)
    await safe_call(callback.message.answer, "Введите новое название списка текстом.")
    await callback.answer()

@router.message(ListInput.rename, F.text)
async def rename_text(message: Message, state: FSMContext):
    if not await check_message(message): return
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 100:
        await safe_call(message.answer, "Название должно быть от 1 до 100 символов.")
        return
    list_id = (await state.get_data())["target_list_id"]
    renamed = rename_list(message.from_user.id, list_id, title)
    await state.clear()
    await safe_call(message.answer, render_list(message.from_user.id, list_id) if renamed else "Список не найден.", reply_markup=list_actions_keyboard(list_id) if renamed else main_menu_for(message.from_user.id))

@router.callback_query(F.data.startswith("list:delete:"))
async def ask_delete(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    list_id = int(callback.data.rsplit(":", 1)[1])
    if not get_list(callback.from_user.id, list_id):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    token = secrets.token_hex(8)
    await state.update_data(delete_token=token, delete_list_id=list_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [styled("🗑 Удалить", f"list:delete_confirm:{token}", "danger")],
        [styled("✅ Отмена", f"list:open:{list_id}", "success")],
    ])
    await safe_call(callback.message.edit_text, "⚠️ Удалить список?\nЭто действие невозможно отменить.", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("list:delete_confirm:"))
async def confirm_delete(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    token = callback.data.rsplit(":",1)[1]
    data = await state.get_data()
    if data.get("delete_token") != token or not data.get("delete_list_id"):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    list_id = data["delete_list_id"]
    await state.update_data(delete_token=None, delete_list_id=None)
    if not delete_list(callback.from_user.id, list_id):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    from database import get_lists
    from keyboards import list_picker
    rows=get_lists(callback.from_user.id)
    await state.clear()
    await safe_call(callback.message.edit_text, f"📋 Мои списки ({len(rows)})",reply_markup=list_picker(rows))
    await callback.answer("Список удалён")

@router.callback_query(F.data.startswith("list:copy:"))
async def copy_list(callback: CallbackQuery):
    if not await check_callback(callback): return
    list_id = int(callback.data.rsplit(":",1)[1])
    body = selected_export(callback.from_user.id, list_id)
    if body is None:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    await safe_call(callback.message.answer, body)
    await callback.answer("Выбранные товары отправлены")

@router.callback_query(F.data.startswith('title:'))
async def title_action(callback:CallbackQuery,state:FSMContext):
    if not await check_callback(callback):return
    _,action,token=callback.data.split(':')
    data=await state.get_data()
    if token!=data.get('pending_title_token'):
        return await callback.answer(STALE_BUTTON,show_alert=True)
    if action=='save':
        title=data['pending_title'];list_id=create_list(callback.from_user.id,title)
        await state.clear()
        d=get_page(callback.from_user.id,list_id)
        await safe_call(callback.message.edit_text,'✅ Список создан.\n\n'+page_text(d),reply_markup=paged_list_keyboard(list_id,d))
    elif action=='edit':
        await state.update_data(pending_title_token=None)
        await state.set_state(ListInput.name)
        await safe_call(callback.message.answer,'Отправьте исправленное название текстом или голосом.')
    elif action in ('cancel','close'):
        await state.clear()
        if action=='close':await safe_call(callback.message.delete)
        else:await safe_call(callback.message.edit_text,'Создание отменено.')
    await callback.answer()

@router.callback_query(F.data.startswith('list:export:'))
async def export_selected(callback:CallbackQuery):
    if not await check_callback(callback):return
    from aiogram.types import BufferedInputFile
    from list_exports import generate
    try:
        _,_,lid,fmt=callback.data.split(':')
        if fmt not in ('txt','xlsx','docx'):return await callback.answer('Неверный формат',show_alert=True)
        payload=generate(callback.from_user.id,int(lid),fmt)
        if payload is None:return await callback.answer(STALE_BUTTON,show_alert=True)
        await safe_call(callback.message.answer_document,BufferedInputFile(payload,filename='purchases.'+fmt),caption='Только отмеченные товары')
        await callback.answer()
    except (ValueError,TypeError):await callback.answer(STALE_BUTTON,show_alert=True)
