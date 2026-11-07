"""Staged store selection: changes are committed only on Save."""
from aiogram import Router,F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup
from access import check_message,check_callback
from database import connect_db
from keyboards import styled
from telegram_retry import safe_call
router=Router(name='shop_settings')
SHOPS=('Пятёрочка','Перекрёсток','Магнит','Дикси','ВкусВилл','Ярче!','Лента','О’КЕЙ','Ашан','METRO','Чижик','Верный','SPAR')
DISCLAIMER='💡 Цены ориентировочные, зависят от региона, магазина, акций и могут изменяться.'
class SettingsInput(StatesGroup):
    city=State();shop=State()
def init_settings():
    with connect_db() as db:
        db.executescript('CREATE TABLE IF NOT EXISTS user_cities(user_id INTEGER PRIMARY KEY,city TEXT NOT NULL); CREATE TABLE IF NOT EXISTS user_shops(user_id INTEGER NOT NULL,shop TEXT NOT NULL,PRIMARY KEY(user_id,shop));')
def get_settings(uid):
    with connect_db() as db:
        r=db.execute('SELECT city FROM user_cities WHERE user_id=?',(uid,)).fetchone()
        return (r[0] if r else None,[x[0] for x in db.execute('SELECT shop FROM user_shops WHERE user_id=? ORDER BY shop',(uid,))])
def body(uid,selected=None):
    city,saved=get_settings(uid);chosen=saved if selected is None else selected
    return '🏪 Магазины и город\n\n📍 Город: '+(city or 'не указан')+'\n🏪 Отмечены: '+(', '.join(chosen) if chosen else 'нет')+'\n\nВыберите магазины, затем нажмите «✅ Сохранить магазины».\n'+DISCLAIMER
def kb(uid,selected=None):
    _,saved=get_settings(uid);chosen=saved if selected is None else selected
    rows=[[styled('📍 Указать город','settings:city')]]
    buttons=[styled(('☑️ ' if s in chosen else '☐ ')+s,f'settings:toggle:{i}') for i,s in enumerate(SHOPS)]
    rows.extend(buttons[i:i+2] for i in range(0,len(buttons),2))
    rows.extend([[styled('➕ Свой магазин','settings:add')],[styled('✅ Сохранить магазины','settings:save','success')],[styled('✖ Отменить выбор','settings:cancel','danger')],[styled('⬅️⬅️ Назад','settings:back','primary'),styled('❌ Закрыть','ui:close','danger')]])
    return InlineKeyboardMarkup(inline_keyboard=rows)
async def show(c,state):
    d=await state.get_data();selected=d.get('shop_draft')
    await safe_call(c.message.edit_text,body(c.from_user.id,selected),reply_markup=kb(c.from_user.id,selected))
@router.message(F.text=='🏪 Мои магазины')
async def settings(m:Message,state:FSMContext):
    if not await check_message(m):return
    await state.clear();await safe_call(m.answer,body(m.from_user.id),reply_markup=kb(m.from_user.id))
@router.callback_query(F.data.startswith('settings:'))
async def callbacks(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    p=c.data.split(':');act=p[1];uid=c.from_user.id
    if act=='city':
        await state.set_state(SettingsInput.city);await safe_call(c.message.answer,'Напишите город, например: Казань.')
    elif act=='add':
        await state.set_state(SettingsInput.shop);await safe_call(c.message.answer,'Введите название собственного магазина.')
    elif act=='toggle' and len(p)==3 and p[2].isdigit() and int(p[2])<len(SHOPS):
        d=await state.get_data();chosen=set(d.get('shop_draft',get_settings(uid)[1]));shop=SHOPS[int(p[2])]
        if shop in chosen:chosen.remove(shop)
        else:chosen.add(shop)
        await state.update_data(shop_draft=sorted(chosen));await show(c,state)
    elif act=='save':
        d=await state.get_data();chosen=d.get('shop_draft',get_settings(uid)[1])
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE');db.execute('DELETE FROM user_shops WHERE user_id=?',(uid,))
            db.executemany('INSERT INTO user_shops(user_id,shop) VALUES(?,?)',[(uid,s) for s in chosen])
        await state.clear();await safe_call(c.message.edit_text,'✅ Магазины сохранены.\n\n'+body(uid),reply_markup=kb(uid))
    elif act=='cancel':
        await state.clear();await safe_call(c.message.edit_text,'Выбор отменён.\n\n'+body(uid),reply_markup=kb(uid))
    elif act=='back':
        from user_preferences import panel,keyboard
        await state.clear();await safe_call(c.message.edit_text,panel(uid),reply_markup=keyboard())
    await c.answer()
@router.message(SettingsInput.city,F.text)
async def city(m:Message,state:FSMContext):
    if not await check_message(m):return
    v=m.text.strip()
    if not 2<=len(v)<=100:return await safe_call(m.answer,'Название города: от 2 до 100 символов.')
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE');db.execute('INSERT INTO user_cities(user_id,city) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET city=excluded.city',(m.from_user.id,v))
    await state.clear();await safe_call(m.answer,body(m.from_user.id),reply_markup=kb(m.from_user.id))
@router.message(SettingsInput.shop,F.text)
async def add_shop(m:Message,state:FSMContext):
    if not await check_message(m):return
    v=m.text.strip()
    if not 2<=len(v)<=80:return await safe_call(m.answer,'Название магазина: от 2 до 80 символов.')
    d=await state.get_data();chosen=set(d.get('shop_draft',get_settings(m.from_user.id)[1]));chosen.add(v)
    await state.set_state(None);await state.update_data(shop_draft=sorted(chosen))
    await safe_call(m.answer,body(m.from_user.id,sorted(chosen)),reply_markup=kb(m.from_user.id,sorted(chosen)))
