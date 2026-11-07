"""Transactional backup of user's lists before any destructive bulk clear."""
import json
from datetime import datetime,timezone
from pathlib import Path
from database import connect_db

def backup_before_clear(uid,directory):
    target=Path(directory);target.mkdir(parents=True,exist_ok=True)
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        lists=[dict(r) for r in db.execute('SELECT * FROM lists WHERE owner_id=? ORDER BY id',(uid,))]
        payload=[]
        for item in lists:
            item['items']=[dict(r) for r in db.execute('SELECT * FROM list_items WHERE list_id=? ORDER BY position,id',(item['id'],))]
            payload.append(item)
        path=target/f'lists_{uid}_{datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}.json'
        # atomic filesystem write before deleting any database data
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
        return path

def clear_with_backup(uid,directory):
    path=backup_before_clear(uid,directory)
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE');db.execute('DELETE FROM lists WHERE owner_id=?',(uid,))
    return path

# The UI calls this only after explicit confirmation by the list owner.
from aiogram import Router,F
from aiogram.filters import Command
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup
from access import check_message,check_callback
from keyboards import styled
from telegram_retry import safe_call
from config import settings
router=Router(name='list_backup')
@router.message(Command('clear_my_lists'))
async def ask_clear(m:Message):
    if not await check_message(m):return
    markup=InlineKeyboardMarkup(inline_keyboard=[[styled('🗑 Да, очистить мои списки','clear:confirm','danger')],[styled('✖ Отмена','clear:cancel')]])
    await safe_call(m.answer,'⚠️ Очистить ВСЕ ваши списки? Сначала будет создана резервная копия. Списки других пользователей не затрагиваются.',reply_markup=markup)
@router.callback_query(F.data.startswith('clear:'))
async def clear_action(c:CallbackQuery):
    if not await check_callback(c):return
    if c.data=='clear:confirm':
        try:
            path=clear_with_backup(c.from_user.id,settings.data_dir/'list_backups')
            await safe_call(c.message.edit_text,'✅ Ваши списки очищены. Резервная копия сохранена в защищённом каталоге бота.')
        except Exception:
            await safe_call(c.message.edit_text,'❌ Не удалось создать копию или очистить списки. Проверьте журнал сервера.')
    else:await safe_call(c.message.edit_text,'Очистка отменена.')
    await c.answer()
