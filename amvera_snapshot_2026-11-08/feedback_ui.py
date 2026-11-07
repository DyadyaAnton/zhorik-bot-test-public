"""User feedback via text or speech, owner triage and replies."""
import asyncio,logging
from aiogram import Router,F,Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup
from access import check_message,check_callback
from config import settings
from database import connect_db
from keyboards import styled
from telegram_retry import safe_call
router=Router(name='feedback_ui')
CATEGORIES={'idea':'💡 Предложение','bug':'🐞 Ошибка','other':'💬 Отзыв или вопрос'}
class Feedback(StatesGroup): text=State();reply=State()
def init():
    with connect_db() as db:db.execute("CREATE TABLE IF NOT EXISTS feedback_tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,source TEXT NOT NULL DEFAULT 'text',status TEXT NOT NULL DEFAULT 'new',reply TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
def kb():return InlineKeyboardMarkup(inline_keyboard=[[styled(label,'fb:new:'+key)] for key,label in CATEGORIES.items()]+[[styled('❌ Закрыть','ui:close','danger')]])
@router.message(Command('feedback'))
@router.message(F.text=='💬 Разработчику')
async def start(m:Message,state:FSMContext):
    if not await check_message(m):return
    await state.clear();await safe_call(m.answer,'💬 Написать разработчику\nВыберите тему. Затем отправьте текст или голосовое. Сообщение и распознанная речь будут доступны владельцу бота.',reply_markup=kb())
@router.callback_query(F.data.startswith('fb:new:'))
async def choose(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    kind=c.data.split(':')[-1]
    if kind not in CATEGORIES:return await c.answer('Неизвестная категория',show_alert=True)
    await state.set_state(Feedback.text);await state.update_data(feedback_kind=kind)
    await safe_call(c.message.edit_text,CATEGORIES[kind]+'\nНапишите сообщение или отправьте голосовое. /cancel — отменить.')
    await c.answer()
@router.message(Feedback.text,F.text)
@router.message(Feedback.text,F.voice)
async def collect(m:Message,state:FSMContext,bot:Bot):
    if not await check_message(m):return
    source='voice' if m.voice else 'text';body=(m.text or '').strip()
    if m.voice:
        if m.voice.file_size and m.voice.file_size>1024*1024:return await m.answer('Голосовое слишком большое (максимум 1 МБ).')
        try:
            from speech import recognize_telegram_voice
            info=await bot.get_file(m.voice.file_id);stream=await bot.download_file(info.file_path)
            body=await asyncio.to_thread(recognize_telegram_voice,stream.read())
        except Exception:
            logging.exception('SpeechKit feedback failed')
            return await m.answer('Не удалось распознать голос. Отправьте текстом.')
    if not body or len(body)>3500:return await m.answer('Сообщение должно содержать от 1 до 3500 символов.')
    data=await state.get_data();kind=data.get('feedback_kind','other')
    with connect_db() as db:
        cur=db.execute('INSERT INTO feedback_tickets(user_id,kind,body,source) VALUES(?,?,?,?)',(m.from_user.id,kind,body,source));ticket=cur.lastrowid
    await state.clear();await safe_call(m.answer,f'✅ Обращение №{ticket} отправлено разработчику. Спасибо!')
    if settings.sandbox_owner_id:
        try:await safe_call(bot.send_message,settings.sandbox_owner_id,f'📨 Обращение №{ticket} · {CATEGORIES[kind]}\nПользователь: {m.from_user.id}\nИсточник: {source}\n\n{body[:2500]}')
        except Exception:logging.exception('Feedback notification failed')
@router.message(Command('admin_feedback'))
async def admin_feedback(m:Message):
    if not m.from_user or m.from_user.id!=settings.sandbox_owner_id:return
    with connect_db() as db:rows=db.execute('SELECT id,user_id,kind,body,status FROM feedback_tickets ORDER BY id DESC LIMIT 10').fetchall()
    if not rows:return await m.answer('Обращений пока нет.')
    for r in rows:
        buttons=InlineKeyboardMarkup(inline_keyboard=[[styled('✉️ Ответить',f'fb:reply:{r["id"]}'),styled('✅ Закрыть',f'fb:close:{r["id"]}','success')]])
        await safe_call(m.answer,f'№{r["id"]} · {CATEGORIES.get(r["kind"],r["kind"])} · {r["status"]}\nОт: {r["user_id"]}\n{r["body"][:1000]}',reply_markup=buttons)
@router.callback_query(F.data.startswith(('fb:reply:','fb:close:')))
async def admin_actions(c:CallbackQuery,state:FSMContext):
    if c.from_user.id!=settings.sandbox_owner_id:return await c.answer('Только владелец',show_alert=True)
    _,act,idstr=c.data.split(':');tid=int(idstr)
    with connect_db() as db:row=db.execute('SELECT id FROM feedback_tickets WHERE id=?',(tid,)).fetchone()
    if not row:return await c.answer('Обращение не найдено',show_alert=True)
    if act=='close':
        with connect_db() as db:db.execute('UPDATE feedback_tickets SET status="closed" WHERE id=?',(tid,))
        await safe_call(c.message.edit_text,c.message.text+'\n✅ Закрыто')
    else:
        await state.set_state(Feedback.reply);await state.update_data(feedback_ticket=tid)
        await safe_call(c.message.answer,f'Введите ответ на обращение №{tid}.')
    await c.answer()
@router.message(Feedback.reply,F.text)
async def send_reply(m:Message,state:FSMContext,bot:Bot):
    if not m.from_user or m.from_user.id!=settings.sandbox_owner_id:return
    d=await state.get_data();tid=d.get('feedback_ticket')
    with connect_db() as db:r=db.execute('SELECT user_id FROM feedback_tickets WHERE id=?',(tid,)).fetchone()
    if not r:return await m.answer('Обращение не найдено.')
    try:await safe_call(bot.send_message,r['user_id'],f'💬 Ответ разработчика на обращение №{tid}:\n\n{m.text[:3500]}')
    except Exception:return await m.answer('Ответ не доставлен. Попробуйте позднее.')
    with connect_db() as db:db.execute('UPDATE feedback_tickets SET status="answered",reply=? WHERE id=?',(m.text[:3500],tid))
    await state.clear();await m.answer('✅ Ответ отправлен.')
