"""Full-list text editor with preview, stable per-user authorization and atomic replacement."""
import re,secrets
from aiogram import Router,F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup
from access import check_message,check_callback
from database import connect_db
from list_storage import get_page
from list_ui import page_text
from keyboards import styled,paged_list_keyboard
from telegram_retry import safe_call
router=Router(name='list_editor')
class Edit(StatesGroup): text=State()

def full_text(uid,lid):
    with connect_db() as db:
        if not db.execute('SELECT 1 FROM lists WHERE id=? AND owner_id=?',(lid,uid)).fetchone():return None
        rows=db.execute('SELECT text FROM list_items WHERE list_id=? ORDER BY position,id',(lid,)).fetchall()
        return '\n'.join(f'{i}. {r[0]}' for i,r in enumerate(rows,1))

def parse_text(text):
    lines=[re.sub(r'^\s*(?:\(\d+\)\s*|\d+\s*[.),:;\-–]\s*|\d+\s+(?!(?:в\s+\d|злаков\b))(?=[^\d\s])|[•-]\s*)','',line).strip() for line in text.splitlines() if line.strip()]
    if any(x.startswith(('✏️ Отредактируйте','📋 Редактирование','Скопируйте текст','Каждая строка —')) for x in lines):
        raise ValueError('В текст попала инструкция бота. Отправьте только строки списка.')
    if len(lines)>200 or any(len(s)>500 for s in lines):raise ValueError('Максимум 200 строк, по 500 символов')
    return lines

def replace(uid,lid,lines):
    """Keep ids, selection and quantity for unchanged items, including duplicates."""
    from collections import defaultdict,deque
    from item_quantity import parse_quantity
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM lists WHERE id=? AND owner_id=?',(lid,uid)).fetchone():return False
        old=db.execute('SELECT id,text FROM list_items WHERE list_id=? ORDER BY position,id',(lid,)).fetchall()
        matched=defaultdict(deque)
        for r in old:matched[r['text']].append(r['id'])
        retained=[]
        for i,raw in enumerate(lines,1):
            name,qty,unit=parse_quantity(raw)
            if matched[name]:
                iid=matched[name].popleft();retained.append(iid)
                db.execute('UPDATE list_items SET position=? WHERE id=?',(i,iid))
            else:
                cur=db.execute('INSERT INTO list_items(list_id,text,position,quantity,unit,selected) VALUES(?,?,?,?,?,?)',(lid,name,i,qty,unit,int(qty>0)))
                retained.append(cur.lastrowid)
        if retained:
            placeholders=','.join('?' for _ in retained)
            db.execute(f'DELETE FROM list_items WHERE list_id=? AND id NOT IN ({placeholders})',(lid,*retained))
        else:
            db.execute('DELETE FROM list_items WHERE list_id=?',(lid,))
        db.execute('UPDATE lists SET updated_at=CURRENT_TIMESTAMP WHERE id=?',(lid,))
    return True

@router.callback_query(F.data.startswith('list:editall:'))
async def start(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    lid=int(c.data.rsplit(':',1)[1]);body=full_text(c.from_user.id,lid)
    if body is None:return await c.answer('Список не найден',show_alert=True)
    await state.clear();await state.update_data(editall_id=lid);await state.set_state(Edit.text)
    await safe_call(c.message.answer,'✏️ Редактирование списка. Скопируйте ТОЛЬКО следующее сообщение, исправьте строки и отправьте обратно. Номера можно оставить. Для отмены: /cancel')
    await safe_call(c.message.answer,body or '(пустой список)')
    await c.answer()

@router.message(Edit.text,F.text)
async def preview(m:Message,state:FSMContext):
    if not await check_message(m):return
    try:lines=parse_text(m.text)
    except ValueError as e:return await safe_call(m.answer,str(e))
    d=await state.get_data();lid=d['editall_id']
    if full_text(m.from_user.id,lid) is None:return await safe_call(m.answer,'Список удалён.')
    token=secrets.token_hex(6);await state.update_data(editall_lines=lines,editall_token=token);await state.set_state(None)
    markup=InlineKeyboardMarkup(inline_keyboard=[[styled('✅ Сохранить',f'editall:save:{token}','success')],[styled('✖ Отмена',f'editall:cancel:{token}','danger')]])
    await safe_call(m.answer,'Проверьте новый список:\n\n'+('\n'.join(f'{i}. {x}' for i,x in enumerate(lines,1)) or '(пустой)')[:3500],reply_markup=markup)

@router.callback_query(F.data.startswith('editall:'))
async def commit(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    _,action,token=c.data.split(':');d=await state.get_data()
    if token!=d.get('editall_token'):return await c.answer('Кнопка устарела',show_alert=True)
    lid=d['editall_id']
    if action=='save':
        ok=replace(c.from_user.id,lid,d['editall_lines']);await state.clear()
        if ok:
            page=get_page(c.from_user.id,lid)
            await safe_call(c.message.edit_text,'✅ Изменения сохранены.\n\n'+page_text(page),reply_markup=paged_list_keyboard(lid,page))
        else:await c.answer('Список не найден',show_alert=True);return
    else:await state.clear();await safe_call(c.message.edit_text,'Редактирование отменено.')
    await c.answer()
