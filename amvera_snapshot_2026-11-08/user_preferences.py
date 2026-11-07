"""Persistent append-only preference catalog with independent active flags."""
import re
from aiogram import Router,F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup
from database import connect_db
from access import check_message,check_callback
from keyboards import styled
from telegram_retry import safe_call
router=Router(name='user_preferences')
TYPES={'like':'❤️ Любимые продукты','dislike':'🚫 Нелюбимые продукты','allergy':'⚠️ Аллергены'}
class PreferenceInput(StatesGroup): value=State()
def rows(uid,kind):
    with connect_db() as db:return db.execute('SELECT id,value,active FROM preferences WHERE telegram_id=? AND preference_type=? ORDER BY value',(uid,kind)).fetchall()
def panel(uid):
    lines=['⚙️ Мои настройки','']
    for k,label in TYPES.items():
        rr=rows(uid,k);lines.append(f'{label}: '+(', '.join(r['value'] for r in rr if r['active']) or 'не указаны'))
    lines.append('\nАллергены учитываются как ограничение. Неизвестный состав не означает безопасность.')
    return '\n'.join(lines)
def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[styled(label,f'prefs:view:{k}')] for k,label in TYPES.items()]+[[styled('❌ Закрыть','ui:close','danger')]])
def category(uid,kind,page=0):
    rr=rows(uid,kind)
    page=max(0,min(page,max(0,(len(rr)-1)//15)))
    start=page*15+1 if rr else 0
    end=min(len(rr),(page+1)*15)
    body=TYPES[kind]+f' ({len(rr)})\nПункты: {start}–{end} / {len(rr)}'
    if not rr:body+='\n\nПока нет продуктов.'
    if kind=='allergy':body+='\n\n⚠️ Снятая отметка не доказывает безопасность продукта.'
    return body

def category_kb(uid,kind,page=0):
    rr=rows(uid,kind);page=max(0,min(page,max(0,(len(rr)-1)//15)))
    active=bool(rr and all(r['active'] for r in rr))
    buttons=[[styled('☑️ Отметить всё' if active else '☐ Отметить всё',
                     f'prefs:all:{kind}:0' if active else f'prefs:all:{kind}:1')]]
    for i,r in enumerate(rr[page*15:(page+1)*15],page*15+1):
        buttons.append([styled('☑️' if r['active'] else '☐',f'prefs:toggle:{kind}:{r["id"]}'),
                        styled(f'{i}. {r["value"][:40]}',f'prefs:toggle:{kind}:{r["id"]}')])
    nav=[]
    if page:nav.append(styled('⬅️⬅️',f'prefs:page:{kind}:{page-1}'))
    if (page+1)*15<len(rr):nav.append(styled('➡️➡️',f'prefs:page:{kind}:{page+1}'))
    if nav:buttons.append(nav)
    buttons += [[styled('➕ Добавить',f'prefs:add:{kind}','success'),styled('🗑 Удалить все',f'prefs:confirm:{kind}','danger')],
                [styled('⬅️⬅️ Назад','prefs:back'),styled('❌ Закрыть','ui:close','danger')]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def parse_values(raw,kind='like'):
    from phrase_parser import parse_phrase
    parsed=parse_phrase(raw,kind)
    return list(parsed.items)

def parsed_preference(raw,kind):
    from phrase_parser import parse_phrase
    return parse_phrase(raw,kind)

def add_values(uid,kind,values):
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        count=db.execute('SELECT COUNT(*) FROM preferences WHERE telegram_id=? AND preference_type=?',(uid,kind)).fetchone()[0]
        if count+sum(1 for value in values if not db.execute('SELECT 1 FROM preferences WHERE telegram_id=? AND preference_type=? AND value=?',(uid,kind,value)).fetchone())>100:raise ValueError('Не более 100 продуктов в категории.')
        for value in values:
            db.execute('INSERT INTO preferences(telegram_id,preference_type,value,active) VALUES(?,?,?,1) ON CONFLICT(telegram_id,preference_type,value) DO UPDATE SET active=1',(uid,kind,value))
@router.message(Command('preferences'))
@router.message(Command('settings'))
@router.message(F.text=='⚙️ Мои настройки')
async def settings(m:Message,state:FSMContext):
    if not await check_message(m):return
    await state.clear();await safe_call(m.answer,panel(m.from_user.id),reply_markup=keyboard())
@router.callback_query(F.data.startswith('prefs:'))
async def callback(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    parts=c.data.split(':');act=parts[1];kind=parts[2] if len(parts)>2 else None
    if act in ('view','edit') and kind in TYPES:
        await safe_call(c.message.edit_text,category(c.from_user.id,kind),reply_markup=category_kb(c.from_user.id,kind))
    elif act=='page' and kind in TYPES and len(parts)==4 and parts[3].isdigit():
        await safe_call(c.message.edit_text,category(c.from_user.id,kind,int(parts[3])),reply_markup=category_kb(c.from_user.id,kind,int(parts[3])))
    elif act=='add' and kind in TYPES:
        await state.set_state(PreferenceInput.value);await state.update_data(preference_type=kind)
        # Edit the existing category card instead of accumulating duplicate prompts.
        await safe_call(c.message.edit_text,
                        'Отправьте продукты текстом или голосом, через запятую. '
                        'Новые продукты ДОБАВЯТСЯ к сохранённым. /cancel — отмена.',
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [styled('⬅️⬅️ Назад',f'prefs:view:{kind}')],
                            [styled('❌ Закрыть','ui:close','danger')]]))
    elif act=='all' and kind in TYPES and len(parts)==4 and parts[3] in ('0','1'):
        target=int(parts[3])
        if kind=='allergy' and target==0:
            return await c.answer('Для аллергенов массовое отключение запрещено: проверьте каждую запись.',show_alert=True)
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE preferences SET active=? WHERE telegram_id=? AND preference_type=?',(target,c.from_user.id,kind))
        await safe_call(c.message.edit_text,category(c.from_user.id,kind),reply_markup=category_kb(c.from_user.id,kind))
    elif act=='toggle' and kind in TYPES and len(parts)==4 and parts[3].isdigit():
        confirm_allergy=False
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT active FROM preferences WHERE telegram_id=? AND preference_type=? AND id=?',(c.from_user.id,kind,int(parts[3]))).fetchone()
            if row:
                if kind=='allergy' and row['active']:
                    confirm_allergy=True
                else:
                    db.execute('UPDATE preferences SET active=? WHERE telegram_id=? AND preference_type=? AND id=?',(1-int(row['active']),c.from_user.id,kind,int(parts[3])))
        if confirm_allergy:
            await safe_call(c.message.edit_text,'⚠️ Отключить аллерген? Это не подтверждает безопасность продукта.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[styled('Да, отключить',f'prefs:disable:{kind}:{parts[3]}','danger')],[styled('Отмена',f'prefs:view:{kind}')]]))
            return await c.answer()
        await safe_call(c.message.edit_text,category(c.from_user.id,kind),reply_markup=category_kb(c.from_user.id,kind))
    elif act=='disable' and kind=='allergy' and len(parts)==4 and parts[3].isdigit():
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE');db.execute('UPDATE preferences SET active=0 WHERE telegram_id=? AND preference_type=? AND id=?',(c.from_user.id,kind,int(parts[3])))
        await safe_call(c.message.edit_text,category(c.from_user.id,kind),reply_markup=category_kb(c.from_user.id,kind))
    elif act=='confirm' and kind in TYPES:
        await safe_call(c.message.edit_text,'Удалить ВСЕ сохранённые позиции категории '+TYPES[kind]+'?',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[styled('🗑 Да, удалить',f'prefs:clear:{kind}','danger')],[styled('❌ Отмена',f'prefs:view:{kind}')]]))
    elif act=='clear' and kind in TYPES:
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE');db.execute('DELETE FROM preferences WHERE telegram_id=? AND preference_type=?',(c.from_user.id,kind))
        await safe_call(c.message.edit_text,category(c.from_user.id,kind),reply_markup=category_kb(c.from_user.id,kind))
    elif act=='shops':
        from shop_settings import body,kb
        await safe_call(c.message.edit_text,body(c.from_user.id),reply_markup=kb(c.from_user.id))
    elif act=='back':await safe_call(c.message.edit_text,panel(c.from_user.id),reply_markup=keyboard())
    await c.answer()
@router.message(PreferenceInput.value,F.text)
async def save(m:Message,state:FSMContext):
    if not await check_message(m):return
    kind=(await state.get_data()).get('preference_type')
    if kind not in TYPES:return
    parsed=parsed_preference(m.text or '',kind)
    if not parsed.items or parsed.uncertain or len(parsed.items)>50 or any(len(v)>80 for v in parsed.items):
        return await m.answer('Не удалось уверенно распознать все продукты. Уточните фразу или перечислите через запятую. Ничего не сохранено. Неясно: '+', '.join(parsed.uncertain)[:300])
    await state.update_data(preference_draft=list(parsed.items))
    await safe_call(m.answer,'Проверим перед сохранением:\n'+ '\n'.join(f'{i}. {v}' for i,v in enumerate(parsed.items,1)),reply_markup=InlineKeyboardMarkup(inline_keyboard=[[styled('✅ Сохранить',f'prefdraft:save:{kind}','success')],[styled('✏️ Исправить',f'prefdraft:edit:{kind}')],[styled('❌ Отмена',f'prefdraft:cancel:{kind}','danger')],[styled('❌ Закрыть',f'prefdraft:close:{kind}','danger')]]))

@router.callback_query(F.data.startswith('prefdraft:'))
async def confirm_preference(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    _,action,kind=c.data.split(':')
    d=await state.get_data()
    if kind not in TYPES or d.get('preference_type')!=kind or not d.get('preference_draft'):
        return await c.answer('Устаревшая кнопка',show_alert=True)
    if action=='save':
        try:add_values(c.from_user.id,kind,d['preference_draft'])
        except ValueError as e:return await c.answer(str(e),show_alert=True)
        await state.clear()
        await safe_call(c.message.edit_text,'✅ Добавлено.\n\n'+category(c.from_user.id,kind),reply_markup=category_kb(c.from_user.id,kind))
    elif action=='edit':
        await state.update_data(preference_draft=None)
        await safe_call(c.message.answer,'Отправьте исправленную фразу текстом или голосом.')
    elif action in ('cancel','close'):
        await state.clear()
        if action=='close':await safe_call(c.message.delete)
        else:await safe_call(c.message.edit_text,'Добавление отменено.')
    await c.answer()
