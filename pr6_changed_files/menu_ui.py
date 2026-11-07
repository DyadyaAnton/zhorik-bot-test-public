"""Telegram menu wizard. No network requests inside DB transactions."""
import asyncio
import json
import logging
import math
import secrets
import sqlite3
import time
from aiogram import Router,F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup,BufferedInputFile
from access import check_message,check_callback
from keyboards import styled,paged_list_keyboard
from telegram_retry import safe_call
from database import connect_db
from list_storage import get_page
from list_ui import page_text
from menu_generator import build as llm_build,MenuInvalid,BudgetExceeded,DAYS,MEALS,context,validate
import menu_rules
from autonomous_menu import build as local_build
from menu_mode import get_mode
from menu_diet import MODES, get as get_diet_mode, set_mode as save_diet_mode, validate_menu as validate_diet, DISCLAIMER as GLUTEN_NOTICE
from recipe_labels import display_title
from menu_budget import get as budget_get, set_budget, get_people, set_people
import menu_library

MEAT_CAP_DEFAULT=7  # режим «меньше мяса»: не более 7 приёмов пищи с мясом из 21 в неделю

router=Router(name='menu_ui')
log=logging.getLogger(__name__)
_tasks={}
_runs={}
_pending_saves=set()
_save_notifications=set()
_variations={}
# End-to-end planning deadline. A timed-out thread may finish its computation,
# but cannot commit a menu: persistence happens only in the awaiting coroutine.
MENU_GENERATION_TIMEOUT_S=180

def cancel_generation(uid,operation_id=None):
    """Cancel only the named operation; never cancel a newer generation."""
    op=_runs.get(uid)
    if not op or (operation_id is not None and op['id']!=operation_id):
        return 'stale'
    if op.get('phase')=='saving':
        return 'saving'
    op['cancelled']=True
    task=op.get('task')
    if task and not task.done():task.cancel()
    if _runs.get(uid) is op:_runs.pop(uid,None)
    if _tasks.get(uid) is task:_tasks.pop(uid,None)
    return 'cancelled'

async def cancel_generation_and_notify(uid):
    op=_runs.get(uid)
    status=cancel_generation(uid)
    if status=='cancelled' and op and op.get('progress'):
        try:
            await safe_call(op['progress'].edit_text,
                            'Генерация отменена. Меню не сохранено.',reply_markup=error_kb())
        except Exception:
            log.warning('Cannot update cancelled menu progress',exc_info=True)
    return status

def error_kb(*,change_budget=False,paths=()):
    rows=[[styled(text,data)] for text,data in paths]
    if change_budget:rows.append([styled('✏️ Ввести свой бюджет','menugen:askbudget')])
    rows.extend([[styled('⬅️⬅️ К настройкам меню','menugen:settings')],
                 [styled('❌ Закрыть','ui:close','danger')]])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _save_generated(uid,people,budget,cal,diet_mode,prefs,menu,stats,expected_revision=None):
    """Serialize outside the write transaction; worker owns its SQLite handle."""
    payload=json.dumps({'menu':menu,'stats':{**stats,'ingredients':[
        [list(k),v] for k,v in stats['ingredients'].items()]}},ensure_ascii=False)
    key=_parameters_key(uid,people,cal,diet_mode,budget,prefs=prefs)
    with connect_db() as db:
        db.execute('PRAGMA busy_timeout = 2000')
        db.execute('BEGIN IMMEDIATE')
        if expected_revision is not None:
            row=db.execute('SELECT value FROM menu_runtime_settings WHERE key=?',
                           (f'u{int(uid)}:settings_rev',)).fetchone()
            current=int(row['value']) if row else 0
            if current!=expected_revision:
                raise MenuInvalid('Настройки питания изменились во время генерации. '
                                  'Меню не сохранено; повторите генерацию.')
        cursor=db.execute('INSERT INTO menus(owner_id,people_count,budget,calorie_target,result_json) VALUES(?,?,?,?,?)',
                          (uid,people,budget,cal,payload))
        menu_library.remember(uid,key,cursor.lastrowid,db=db)
        return cursor.lastrowid


def _reprice_saved_menu(uid,menu_id):
    """Read, validate and commit price snapshot off the Telegram event loop."""
    settings_revision=menu_library.settings_rev(uid)
    with connect_db() as db:
        row=db.execute('SELECT * FROM menus WHERE id=? AND owner_id=?',(menu_id,uid)).fetchone()
    if not row:
        return None
    saved=json.loads(row['result_json'])
    products,prefs,prices,_=context(uid)
    new_stats=validate(saved['menu'],products,prefs,row['people_count'],prices)
    validate_diet(saved['menu'],saved['stats'].get('diet_mode','regular'))
    new_stats.update({k:saved['stats'][k] for k in ('mode','diet_mode','multi_dish','target_verified','budget_verified') if k in saved['stats']})
    # Repricing creates a new menu version, not a new nutrition calculation.
    # Preserve the immutable energy record (or the absence of one for legacy menus).
    from menu_energy_snapshot import saved_energy_snapshot
    energy_snapshot=saved_energy_snapshot(saved['menu'],saved['stats'])
    if energy_snapshot is not None:
        new_stats['energy_snapshot']=energy_snapshot
        new_stats['recipe01_meal_balance']=saved['stats'].get('recipe01_meal_balance')
        new_stats['known_kcal']=saved['stats']['known_kcal']
        new_stats['missing_kcal']=saved['stats']['missing_kcal']
    else:
        # Old menus remain old menus, even though repricing uses today's catalog.
        new_stats['known_kcal']=saved['stats'].get('known_kcal',0)
        new_stats['missing_kcal']=saved['stats'].get('missing_kcal',[])
    over_limit=bool(new_stats['missing_prices'] or new_stats['missing_purchase_prices']
                    or menu_rules.is_over_budget(new_stats['purchase_estimate'],row['budget']))
    new_menu=json.dumps({'menu':saved['menu'],
        'stats':{**new_stats,'ingredients':[[list(k),v] for k,v in new_stats['ingredients'].items()]}},
        ensure_ascii=False)
    with connect_db() as db:
        db.execute('PRAGMA busy_timeout = 2000')
        db.execute('BEGIN IMMEDIATE')
        # The menu and its ownership may change between read and commit.
        still_owned=db.execute('SELECT 1 FROM menus WHERE id=? AND owner_id=?',(menu_id,uid)).fetchone()
        if not still_owned:
            raise MenuInvalid('Меню больше недоступно.')
        current=db.execute('SELECT value FROM menu_runtime_settings WHERE key=?',
                           (f'u{int(uid)}:settings_rev',)).fetchone()
        if (int(current['value']) if current else 0)!=settings_revision:
            raise MenuInvalid('Настройки питания изменились во время пересчёта. Повторите операцию.')
        cur=db.execute('INSERT INTO menus(owner_id,people_count,budget,calorie_target,result_json) VALUES(?,?,?,?,?)',
            (uid,row['people_count'],row['budget'],row['calorie_target'],new_menu))
        fresh_id=cur.lastrowid
        db.execute('INSERT INTO zm_menu_price_versions(parent_menu_id,new_menu_id) VALUES(?,?)',
                   (menu_id,fresh_id))
        if not over_limit:
            db.execute('UPDATE zm_menu_latest SET menu_id=? WHERE owner_id=? AND menu_id=?',
                       (fresh_id,uid,menu_id))
    return fresh_id,new_stats,row['budget'],over_limit


def _save_menu_shopping_list(uid,menu_id):
    """One transaction for idempotent shopping-list creation, off event loop."""
    with connect_db() as db:
        db.execute('PRAGMA busy_timeout = 2000')
        db.execute('BEGIN IMMEDIATE')
        record=db.execute('SELECT result_json FROM menus WHERE id=? AND owner_id=?',(menu_id,uid)).fetchone()
        if not record:
            return None
        data=json.loads(record['result_json'])
        existing=db.execute('SELECT m.list_id FROM menu_shopping_lists m JOIN lists l ON l.id=m.list_id WHERE m.menu_id=? AND l.owner_id=?',(menu_id,uid)).fetchone()
        if existing:
            return existing['list_id']
        db.execute('DELETE FROM menu_shopping_lists WHERE menu_id=?',(menu_id,))
        menu_row=db.execute('SELECT people_count,budget,calorie_target FROM menus WHERE id=?',(menu_id,)).fetchone()
        ordinal=db.execute('SELECT COUNT(*) FROM menus WHERE owner_id=? AND id<=?',(uid,menu_id)).fetchone()[0]
        title=f"🍽 {menu_row['people_count']}ч/{menu_row['budget']:g}₽/{menu_row['calorie_target']}кк — №{ordinal}"
        cur=db.execute('INSERT INTO lists(owner_id,title,tags) VALUES(?,?,?)',(uid,title,'еда'))
        list_id=cur.lastrowid
        items=data['stats']['ingredients']
        db.executemany('INSERT INTO list_items(list_id,text,position,quantity,unit,selected) VALUES(?,?,?,?,?,1)',
            [(list_id,k[0],i,int(math.ceil((v-1e-9)/10)*10) if k[1]=='г' else round(v,3),k[1])
             for i,(k,v) in enumerate(items,1)])
        db.execute('INSERT INTO menu_shopping_lists(menu_id,list_id) VALUES(?,?)',(menu_id,list_id))
        return list_id


async def _report_detached_save(op,save_task):
    """Report a completed commit even if the parent coroutine was cancelled."""
    try:
        menu_id=save_task.result()
    except asyncio.CancelledError:
        log.error('Detached save task cancelled; SQLite commit outcome is unknown')
        text='⚠️ Проверить завершение сохранения не удалось. Перед повторной генерацией откройте историю меню.'
    except Exception:
        log.exception('Detached menu save failed after parent task cancellation')
        text='⚠️ Сохранение меню завершилось ошибкой. Проверьте историю перед повторным запуском.'
    else:
        log.warning('Detached save completed for menu_id=%s',menu_id)
        text=f'✅ Меню №{menu_id} сохранено. Откройте историю меню.'
    progress=op.get('progress')
    if progress is not None:
        try:
            await safe_call(progress.answer,text,reply_markup=error_kb())
        except Exception:
            log.exception('Cannot notify user about detached menu commit')


def _observe_save(op,save_task):
    _pending_saves.discard(save_task)
    if not op.get('orphaned') or op.get('notified'):return
    op['notified']=True
    task=asyncio.create_task(_report_detached_save(op,save_task))
    _save_notifications.add(task)
    task.add_done_callback(_save_notifications.discard)


async def wait_for_pending_saves(timeout=20):
    """Drain observed writes before Bot.session is closed on polling exit."""
    if _pending_saves:
        await asyncio.wait(tuple(_pending_saves),timeout=timeout)
    await asyncio.sleep(0)
    if _save_notifications:
        await asyncio.wait(tuple(_save_notifications),timeout=5)
    if _pending_saves:
        log.error('Shutdown: %s menu save operations still active',len(_pending_saves))

async def _within_generation_deadline(awaitable,deadline):
    remaining=deadline-time.monotonic()
    if remaining<=0:
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        elif isinstance(awaitable,asyncio.Future):
            awaitable.cancel()
        raise asyncio.TimeoutError
    return await asyncio.wait_for(awaitable,timeout=remaining)

def menu_variation(uid):
    _variations[uid]=_variations.get(uid,0)+1
    return _variations[uid]
class MenuWizard(StatesGroup):
    budget=State()
    calories=State()
    favorite_title=State()

def kb(people=1,cal=None,diet_mode='regular',revision=0,has_saved=False):
    rows=[
      [styled(('✅ ' if i==people else '')+str(i)+' чел.',f'menugen:people:{i}:{revision}') for i in (1,2,3)],
      [styled(('✅ ' if diet_mode==mode else '')+label,f'menugen:diet:{mode}:{revision}') for mode,label in (('regular','Обыч.'),('pp','🥗 ПП'),('gluten_avoid','🌾 Без глют.'))],
      [styled('💰 Бюджет',f'menugen:budget:{revision}'),styled('🔥 Калории',f'menugen:calories:{revision}')],
    ]
    if has_saved:rows.append([styled('📖 Открыть меню','menugen:latest')])
    rows.append([styled('🪄 Сгенерировать меню',f'menugen:run:{revision}','success')])
    rows.append([styled('❤️ Любимые меню','menugen:favorites'),styled('📚 Рецепты','recipes:home')])
    rows.append([styled('❌ Закрыть','ui:close','danger')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fast_menu_snapshot(uid):
    """UI-only reads. Never load products or planning/retail prices here.

    The same preferences and calorie source as menu_generator.context(),
    without invoking prices_for_menu() or constructing the product catalog.
    """
    with connect_db() as db:
        prefs={k:[r['value'].casefold() for r in db.execute(
            'SELECT value FROM preferences WHERE telegram_id=? AND preference_type=? AND active=1',
            (uid,k))] for k in ('allergy','dislike','like')}
        row=db.execute('SELECT calorie_target FROM user_settings WHERE telegram_id=?',(uid,)).fetchone()
    return prefs,(row['calorie_target'] if row else None)


def _card_snapshot(uid):
    """Read all card settings through one SQLite connection, without FSM writes.

    This is UI state only: never read product catalogs, retailer offers or prices.
    """
    with connect_db() as db:
        prefs={k:[] for k in ('allergy','dislike','like')}
        for row in db.execute('SELECT preference_type,value FROM preferences WHERE telegram_id=? AND active=1',(uid,)):
            if row['preference_type'] in prefs:
                prefs[row['preference_type']].append(row['value'].casefold())
        calorie=db.execute('SELECT calorie_target FROM user_settings WHERE telegram_id=?',(uid,)).fetchone()
        people=db.execute('SELECT people FROM zm_menu_people WHERE user_id=?',(uid,)).fetchone()
        budget=db.execute('SELECT budget FROM zm_menu_budget WHERE user_id=?',(uid,)).fetchone()
        if budget is None:
            budget=db.execute('SELECT budget FROM menus WHERE owner_id=? AND budget>0 ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
        mode=db.execute('SELECT mode FROM zm_menu_modes WHERE user_id=?',(uid,)).fetchone()
        n=int(people['people']) if people else 1
        n=n if n in (1,2,3) else 1
        diet=mode['mode'] if mode and mode['mode'] in MODES else 'regular'
        value=float(budget['budget']) if budget else None
        value=value if value is not None and math.isfinite(value) and 100<=value<=1000000 else None
        cal=calorie['calorie_target'] if calorie else None
        key=menu_library.key_for(n,value or 0,cal,diet,prefs)
        saved=db.execute('SELECT 1 FROM zm_menu_latest WHERE owner_id=? AND parameters_key=? LIMIT 1',(uid,key)).fetchone()
        try:
            meat=db.execute('SELECT value FROM menu_runtime_settings WHERE key=?',(f'u{int(uid)}:meat_cap',)).fetchone()
        except sqlite3.OperationalError:
            meat=None
    meat_cap=menu_library.option_parse_int(meat['value']) if meat else None
    return {'people':n,'budget':value,'diet':diet,'cal':cal,'prefs':prefs,'has_saved':bool(saved),'meat_cap':meat_cap}


def _card_parts(snapshot,revision):
    people,budget,cal,diet=(snapshot[k] for k in ('people','budget','cal','diet'))
    text=(f'🍽 Меню на неделю\n👥 {people} чел. · {MODES[diet]}\n'
          +(f'💰 {budget:g} ₽/нед.' if budget else '💰 Бюджет не задан')
          +f' · 🔥 {cal or "не задано"} ккал/чел.')
    if snapshot.get('meat_cap') is not None:
        text+=f'\n🥦 Меньше мяса: не более {snapshot["meat_cap"]} приёмов в неделю (на одно следующее меню)'
    return text,kb(people,cal,diet,revision,snapshot['has_saved'])


async def open_settings_from_inline(message,uid):
    """Replace an inline screen with the menu settings; bind the actual message."""
    snapshot=_card_snapshot(uid)
    revision=menu_library.next_card(uid,message.chat.id)
    body,markup=_card_parts(snapshot,revision)
    try:
        await message.edit_text(body,reply_markup=markup)
    except Exception as exc:
        if 'message is not modified' in str(exc).lower():
            menu_library.card_bind(uid,message.chat.id,revision,message.message_id)
            return message
        log.warning('Cannot edit previous card; sending menu settings',exc_info=True)
        card=await safe_call(message.answer,body,reply_markup=markup)
        if card:
            menu_library.card_bind(uid,message.chat.id,revision,card.message_id)
        return card
    menu_library.card_bind(uid,message.chat.id,revision,message.message_id)
    return message


def _parameters_key(uid,people=None,cal=None,diet=None,budget=None,prefs=None):
    if prefs is None or cal is None:
        loaded_prefs,target=_fast_menu_snapshot(uid)
        if prefs is None:prefs=loaded_prefs
        if cal is None:cal=target
    return menu_library.key_for(people if people is not None else get_people(uid),
                                budget if budget is not None else (_last_budget(uid) or 0),
                                cal, diet if diet is not None else get_diet_mode(uid), prefs)


def _current_token(uid):
    """Отметка текущих настроек пользователя (включая редакцию настроек) для кнопок путей выхода."""
    snap=_card_snapshot(uid)
    return menu_rules.params_token(snap['people'],snap['budget'] or 0,snap['cal'] or 0,snap['diet'],menu_library.settings_rev(uid))


def _card_is_current(c):
    """SQLite-backed revision, survives Amvera restarts; callback embeds revision."""
    chunks=(c.data or '').split(':')
    if not chunks or not chunks[-1].isdigit():return False
    return menu_library.card_valid(c.from_user.id,c.message.chat.id,
                                   int(chunks[-1]),c.message.message_id)


def _last_budget(uid):
    return budget_get(uid)

def _remember_budget(uid,value):
    set_budget(uid,value)

def _ai_owner(uid):
    from config import settings
    return settings.sandbox_owner_id is not None and uid == settings.sandbox_owner_id


def result_kb(menu_id, uid=None):
    favorite=bool(uid is not None and menu_library.favorite_for_menu(uid,menu_id))
    heart='❤️ Любимые' if favorite else '🤍 Любимые'
    return InlineKeyboardMarkup(inline_keyboard=[
      [styled('📅 По дням',f'menugen:day:{menu_id}:0'),styled('🧮 Смета',f'menugen:estimate:{menu_id}'),styled('🛒 Покупки',f'menugen:list:{menu_id}')],
      [styled(heart,f'menugen:favadd:{menu_id}'),styled('💰 Цены',f'menugen:reprice:{menu_id}'),styled('ℹ️ Расчёты','menugen:info')],
      *([[styled('🧠 Проверить через ИИ',f'menugen:ai_review:{menu_id}')]] if uid is not None and _ai_owner(uid) else []),
      [styled('📄 TXT',f'menugen:export:{menu_id}:txt'),styled('📑 DOCX',f'menugen:export:{menu_id}:docx')],
      [styled('🔄 Заново','menugen:again'),styled('❌ Закрыть','ui:close','danger')]])


def round_grams(menu):
    """Round displayed recipe portions UP; purchase totals derive from these portions."""
    for daily in menu['menu'].values():
        for dishes in daily.values():
            for dish in dishes:
                for item in dish['ingredients']:
                    if item['unit']=='г':
                        item['amount']=int(math.ceil((float(item['amount'])-1e-9)/10)*10)
    return menu


# Добавки к блюдам проверяются на бюджет ДО округления порций, а округление вверх на 10 г может вывести итог за допуск.
# Поэтому при нарушении ЛЮБОЙ обязательной проверки (бюджет, калории, тип питания, аллергены, состав) пробуем меньше добавок,
# а в крайнем случае меню без добавок (оно проходит те же проверки и при ошибке отказывает).
EXPAND_BUDGET_SCALES=(1.0,0.98,0.96,0.93,0.9,None)


def _finalize_generated(menu,stats,products,prefs,prices,people,budget,cal,diet_mode):
    """CPU/SQLite-heavy validation: called in a worker, never on Telegram's loop."""
    import copy
    from menu_components import expand
    base=menu
    for scale in EXPAND_BUDGET_SCALES:
        last=scale is None
        try:
            if last:
                menu,multi_dish=copy.deepcopy(base),False
            else:
                menu,multi_dish=expand(base,stats,products,prefs,people,prices,
                                       diet_mode=diet_mode,budget=budget*scale)
            round_grams(menu)
            new_stats=validate(menu,products,prefs,people,prices)
            validate_diet(menu,diet_mode)
        except MenuInvalid:
            if last:raise
            continue  # добавки нарушили обязательную проверку (аллерген, тип питания, состав): пробуем меньше добавок
        priced=not (new_stats['missing_prices'] or new_stats['missing_purchase_prices'])
        target_ok=(not new_stats['missing_kcal'] and abs(new_stats['known_kcal']/people/7-cal)<=cal*.1)
        over=priced and menu_rules.is_over_budget(new_stats['purchase_estimate'],budget)
        if (over or not target_ok) and not last:
            continue  # после округления вышли за допуск бюджета или калорий: меньше добавок
        break
    new_stats['multi_dish']=multi_dish
    validate_diet(menu,diet_mode)
    new_stats['mode']=stats.get('mode')
    new_stats['diet_mode']=diet_mode
    # RECIPE-01 soft energy-balance diagnostics operate on the actual
    # finalized gram amounts. Estimated kcal are NOT verified full KBJU.
    from menu_energy_snapshot import build_energy_snapshot, balance_from_snapshot
    catalog={ ' '.join(p['name'].casefold().replace('ё','е').split()):p for p in products }
    snapshot=build_energy_snapshot(menu,catalog,people=people)
    new_stats['energy_snapshot']=snapshot
    new_stats['recipe01_meal_balance']=balance_from_snapshot(snapshot,cal)
    # The generation gate and every subsequent consumer now use the same
    # reviewed PRICE07 nutrition lookup on the rounded final portions.
    ingredient_count=sum(len(dish['ingredients']) for daily in menu['menu'].values()
                         for dishes in daily.values() for dish in dishes)
    # PRICE07 rounds each ingredient to 0.1 kcal. The existing validator
    # accumulates those values and rounds only the all-people total.
    rounding_bound=0.051*(ingredient_count*people+1)
    if (snapshot['week']['kcal'] is None or
            abs(snapshot['week']['known_kcal']*people-new_stats['known_kcal'])>rounding_bound):
        raise MenuInvalid('Не удалось согласовать расчёт калорийности меню.')
    new_stats['known_kcal']=round(snapshot['week']['known_kcal']*people,1)
    new_stats['target_verified']=(not new_stats['missing_kcal'] and
        abs(new_stats['known_kcal']/people/7-cal)<=cal*.1)
    if new_stats['mode']=='local':new_stats['budget_verified']=False
    if not new_stats['target_verified']:
        raise MenuInvalid(f'Цель {cal:g} ккал/сутки недостижима после округления порций; попробуйте сформировать меню заново.')
    if new_stats['missing_prices'] or new_stats['missing_purchase_prices']:
        raise MenuInvalid('Нет цен для полной проверки закупки: '+
                          ', '.join((new_stats['missing_purchase_prices'] or new_stats['missing_prices'])[:6]))
    if menu_rules.is_over_budget(new_stats['purchase_estimate'],budget):
        raise BudgetExceeded(f'Бюджет {budget:g} ₽: расчёт расхода продуктов '
                             f'{new_stats["purchase_estimate"]:.2f} ₽. '
                             f'Превышение более {menu_rules.BUDGET_TOLERANCE*100:.0f}%. '
                             'Меню не сохранено. Измените условия или бюджет.',budget,new_stats['purchase_estimate'])
    return menu,new_stats


def _qty(item):
    return f"{item['amount']:g} {item['unit']}"


def day_text(menu, day_index, people, calories, stats=None):
    day=DAYS[day_index]
    lines=[f'🍽 {day} · {people} чел. · цель {calories} ккал/чел.']
    from menu_energy_snapshot import saved_energy_snapshot
    snapshot=saved_energy_snapshot(menu,stats)
    daily_kcal=0.0
    unknown=snapshot is None
    for meal in MEALS:
        lines.append('\n'+meal)
        for dish_index,dish in enumerate(menu['menu'][day][meal]):
            lines.append('• '+display_title(dish['name']))
            row=snapshot['days'][day]['meals'][meal]['dishes'][dish_index] if snapshot else None
            for item in dish['ingredients']:
                lines.append(f"  {item['product']} — {_qty(item)} на человека")
            if row and row['kcal'] is not None:
                lines.append(f"  ≈ {row['kcal']:.0f} ккал/чел.")
                daily_kcal+=row['kcal']
            else:unknown=True
    lines.append(f'\n🔥 За день: ≈ {snapshot["days"][day]["kcal"]:.0f} ккал/чел.' if snapshot and snapshot['days'][day]['kcal'] is not None else '\n🔥 Калорийность дня не полностью известна'+(' (старое меню без снимка)' if snapshot is None else ''))
    lines.append('Вес ингредиентов указан до приготовления; порции округлены вверх до 10 г.')
    return '\n'.join(lines)



def telegram_chunks(text,limit=3900):
    """Split on line boundaries without losing content or exceeding Telegram limit."""
    if limit<100:raise ValueError('Слишком маленький лимит')
    pieces=[];current=''
    for line in text.splitlines(keepends=True):
        while len(line)>limit:
            if current:pieces.append(current);current=''
            pieces.append(line[:limit]);line=line[limit:]
        if len(current)+len(line)>limit:
            if current:pieces.append(current)
            current=''
        current+=line
    if current:pieces.append(current)
    return pieces or ['']

async def _show_long(message,body,markup,*,edit=True):
    """No silent truncation; controls remain on the final part."""
    parts=telegram_chunks(body,3700)
    intermediate=InlineKeyboardMarkup(inline_keyboard=[
        [styled('❌ Закрыть','ui:close','danger')]])
    first=parts[0]
    if len(parts)>1:first+='\n\n(продолжение ниже)'
    if edit:await safe_call(message.edit_text,first,reply_markup=markup if len(parts)==1 else intermediate)
    else:await safe_call(message.answer,first,reply_markup=markup if len(parts)==1 else intermediate)
    for idx,part in enumerate(parts[1:],1):
        await safe_call(message.answer,f'Продолжение {idx+1}/{len(parts)}\n'+part,
                        reply_markup=markup if idx==len(parts)-1 else intermediate)


def day_kb(menu_id,day_index):
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('⬅️',f'menugen:day:{menu_id}:{(day_index-1)%7}'),styled(f'{day_index+1}/7',f'menugen:day:{menu_id}:{day_index}'),styled('➡️',f'menugen:day:{menu_id}:{(day_index+1)%7}')],
        [styled('🧮 Смета',f'menugen:estimate:{menu_id}')],
        [styled('📄 TXT',f'menugen:export:{menu_id}:txt'),styled('🖨 DOCX',f'menugen:export:{menu_id}:docx')],
        [styled('🛒 Создать покупки из меню',f'menugen:list:{menu_id}','success')],
        [styled('⬅️⬅️ Назад',f'menugen:summary:{menu_id}')],
        [styled('❌ Закрыть','ui:close','danger')]])


def estimate_text(stats,budget):
    missing=stats.get('missing_purchase_prices', stats.get('missing_prices',[]))
    planned=stats.get('purchase_estimate',stats['known_cost'])
    details=stats.get('price_provenance') or []
    without_market=sum(row.get('quality')!='verified_market' for row in details) if details else len(stats.get('estimated_prices',[]))
    lines=['🧮 Ориентировочная смета закупки',f'Стоимость по расходу (без целых упаковок): {planned:g} ₽',
           f'Стоимость использованного количества: {stats["known_cost"]:g} ₽',
           f'Недельный бюджет: {budget:g} ₽',
           f'Позиций без рыночного подтверждения: {without_market}']
    if details:
        counts={}
        for row in details:
            quality=row.get('quality','unknown')
            counts[quality]=counts.get(quality,0)+1
        lines.append('Источники: '+', '.join(f'{label} — {counts.get(key,0)}' for key,label in (
            ('verified_market','подтверждённые магазинные'),('personal','личные'),
            ('planning_assumption','условные'),('missing','отсутствуют'),('unknown','неизвестные')) if counts.get(key)))
        verified=[row for row in details if row.get('quality')=='verified_market']
        for row in verified[:10]:
            lines.append('✓ '+str(row.get('product',''))+': '+str(row.get('retailer','магазин'))+
                         ' · '+str(row.get('region','регион не указан'))+
                         ' · '+str(row.get('observed_at','дата не указана'))[:10])
        if len(verified)>10:lines.append(f'… ещё {len(verified)-10} подтверждённых позиций')
    if missing:
        lines.extend([f'⚠️ Бюджет не подтверждён: нет цен на {len(missing)} продуктов.', 'Без цен: '+', '.join(missing)])
    else:
        if planned>budget:
            lines.append(f'⚠️ Превышение: {planned-budget:.2f} ₽ ({(planned/budget-1)*100:.1f}%).')
        else:lines.append(f'Ориентировочный остаток: {budget-planned:.2f} ₽')
    lines.append('⚠️ Цены ориентировочные; реальная сумма на кассе может отличаться · ℹ️ О расчётах')
    return '\n'.join(lines)

@router.message(F.text=='🍽 Меню на неделю')
async def start(m:Message,state:FSMContext):
    if not await check_message(m):return
    await state.clear()
    snapshot=_card_snapshot(m.from_user.id)
    revision=menu_library.next_card(m.from_user.id,m.chat.id)
    body,markup=_card_parts(snapshot,revision)
    card=await safe_call(m.answer,body,reply_markup=markup)
    if card:
        menu_library.card_bind(m.from_user.id,m.chat.id,revision,card.message_id)
        await state.update_data(menu_card_id=card.message_id,menu_chat_id=card.chat.id)

@router.message(MenuWizard.budget,F.text)
async def budget(m:Message,state:FSMContext):
    if not await check_message(m):return
    try:value=float(m.text.strip().replace(' ', '').replace(',','.'))
    except ValueError:value=0
    if not 100<=value<=1000000:return await m.answer('Введите бюджет от 100 до 1 000 000 рублей.')
    try:_remember_budget(m.from_user.id,value)
    except ValueError as exc:return await m.answer(str(exc))
    await state.update_data(menu_budget=value)
    await state.set_state(None)
    await refresh_menu_card(m,state,uid=m.from_user.id)

@router.message(MenuWizard.calories,F.text)
async def set_calories(m:Message,state:FSMContext):
    if not await check_message(m):return
    raw=m.text.strip()
    if not raw.isdecimal():
        return await safe_call(m.answer,'Введите калорийность целым числом от 500 до 5000 ккал, например 2000.')
    value=int(raw)
    if not 500<=value<=5000:
        return await safe_call(m.answer,'Введите калорийность от 500 до 5000 ккал в сутки на человека.')
    uid=m.from_user.id
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO users(telegram_id) VALUES(?)',(uid,))
        db.execute('INSERT INTO user_settings(telegram_id,calorie_target) VALUES(?,?) ON CONFLICT(telegram_id) DO UPDATE SET calorie_target=excluded.calorie_target, updated_at=CURRENT_TIMESTAMP',(uid,value))
    await state.set_state(None)
    await refresh_menu_card(m,state,uid=uid)


@router.message(MenuWizard.favorite_title,F.text)
async def favorite_title(m:Message,state:FSMContext):
    if not await check_message(m):return
    data=await state.get_data()
    try:menu_library.favorite_rename(m.from_user.id,int(data.get('rename_favorite_id')),m.text)
    except (ValueError,TypeError) as exc:
        return await safe_call(m.answer,str(exc))
    await state.set_state(None)
    return await safe_call(m.answer,'❤️ Название любимого меню сохранено.')

async def refresh_menu_card(message,state,uid=None):
    # A callback's message.from_user is the BOT, not the person clicking.
    # Always pass the callback's real user ID, then prefer persisted SQLite.
    if uid is None:
        # Only user-originating Message may use the default. Callbacks must
        # supply callback.from_user.id (message.from_user is the BOT).
        if getattr(message.from_user,'is_bot',False):
            raise ValueError('Для callback нужен ID нажавшего пользователя')
        uid=message.from_user.id
    snapshot=_card_snapshot(uid)
    info=menu_library.card_info(uid,message.chat.id)
    revision=int(info['revision']) if info else menu_library.next_card(uid,message.chat.id)
    mid=info['message_id'] if info else None
    body,markup=_card_parts(snapshot,revision)
    if mid:
        try:
            started=time.perf_counter()
            try:
                await message.bot.edit_message_text(body,chat_id=message.chat.id,message_id=mid,reply_markup=markup)
            finally:
                from perf_diagnostics import mark
                mark('menu_card_edit',time.perf_counter()-started)
            return
        except Exception as exc:
            if 'message is not modified' in str(exc).lower():return
            log.warning('Menu card update failed; sending replacement',exc_info=True)
    revision=menu_library.next_card(uid,message.chat.id)
    body,markup=_card_parts(snapshot,revision)
    card=await safe_call(message.answer,body,reply_markup=markup)
    if card:
        menu_library.card_bind(uid,message.chat.id,revision,card.message_id)
        await state.update_data(menu_card_id=card.message_id,menu_chat_id=card.chat.id)


def summary(menu,stats,people,budget):
    from menu_energy_snapshot import saved_energy_snapshot
    snapshot=saved_energy_snapshot(menu,stats)
    lines=[f'🍽 Меню на неделю · {people} чел. · бюджет {budget:g} ₽',
           'Тип питания: '+MODES.get(stats.get('diet_mode','regular'),'🍽 Обычное'),
           '📅 Состав, граммовки и калории — по дням.']
    for day in DAYS:
        lines.append('\n'+day)
        for meal in MEALS:
            lines.append(meal+': '+', '.join(display_title(x['name']) for x in menu['menu'][day][meal]))
    indicative=stats.get('purchase_estimate',stats['known_cost'])
    over=indicative-budget if budget else 0
    lines.extend(['\nОриентировочная закупка: '+(f'{indicative:.2f} ₽ (неполная)' if stats['missing_prices'] else f'{indicative:.2f} ₽'),
                  '(по расходу всех ингредиентов, включая яйца, молочное и хлеб; не сумма на кассе)',
                  f'Стоимость использованных ингредиентов: {stats["known_cost"]:.2f} ₽',
                  f'Расчётных цен без рыночного подтверждения: {len(stats.get("estimated_prices",[]))}',
                  f'Без цены: {len(stats["missing_prices"])} продуктов',
                  (f'Средняя калорийность: {snapshot["week"]["kcal"] / 7:.0f} ккал на человека в день'
                   if snapshot and snapshot['week']['kcal'] is not None else
                   'Средняя калорийность: не полностью известна'+(' (старое меню без снимка)' if snapshot is None else '')),
                  '⚠️ Расчёты ориентировочные · ℹ️ О расчётах'])
    if stats.get('recipe01_meal_balance'):
        values=[entry for day in stats['recipe01_meal_balance'].values() for entry in day.values()]
        deviations=sum(x['status']=='soft_deviation' for x in values)
        incomplete=sum(x['status']=='incomplete' for x in values)
        lines.append(f'🥣 Распределение калорий 27/38/35: мягких отклонений {deviations}, '
                     f'без расчёта {incomplete} из {len(values)} приёмов (оценка).')
        lines.append('Белки, жиры и углеводы: не подтверждены для этого меню; точные КБЖУ не заявляются.')
    if over > 0 and budget:
        lines.append(f'⚠️ Превышение бюджета: {over:.2f} ₽ ({over/budget*100:.1f}%).')
        if over / budget > menu_rules.BUDGET_TOLERANCE:
            lines.append(f'Превышение более {menu_rules.BUDGET_TOLERANCE*100:.0f}%: рекомендуем уменьшить стоимость корзины или изменить бюджет.')
        else:
            lines.append(f'Допуск +{menu_rules.BUDGET_TOLERANCE*100:.0f}% применён к ориентировочной цене.')
    if not stats.get('multi_dish',False):
        lines.append('ℹ️ Упрощённое меню: дополнительные блюда не прошли проверку бюджета, калорий или ограничений.')
    else:
        lines.append('ℹ️ Для добавления блюд граммовки исходных порций могли быть уменьшены; подробности — по дням.')
    qwen_status=stats.get('qwen_review_status')
    if qwen_status=='reviewed':
        lines.append('🧠 Qwen провёл редакторский обзор; расчёты и ограничения проверены отдельно Жориком.')
        if stats.get('qwen_review_issues'):
            lines.append('⚠️ У Qwen остались замечания по разнообразию; меню не является редакционно утверждённым.')
    elif qwen_status in ('unavailable_error','unavailable_limit'):
        lines.append('⚠️ Qwen сейчас недоступен. Меню проверено локально, но ИИ-обзор не выполнялся.')
    if stats.get('packaging_assumed'):
        pass  # Explained in the common calculation notice.
    if stats.get('diet_mode')=='gluten_avoid':lines.append('⚠️ Безглютеновый состав и следы глютена не проверены; сверяйте упаковки.')
    if stats.get('diet_mode')=='pp':lines.append('🥗 ПП — предварительный подбор, не лечебная диета.')
    if stats.get('mode')=='local':
        lines.append('🧮 Автономный подбор · ориентировочная смета.')
        if stats.get('missing_kcal'):
            lines.append('Калорийная цель: не проверена — нет данных о калорийности.')
        else:
            lines.append('Калорийная цель подтверждена: '+('да' if stats.get('target_verified') else 'нет'))
    return '\n'.join(lines)

async def _display_summary(message,menu_id,menu,stats,people,budget,edit=True,uid=None):
    """All lines survive Telegram's 4096-character limit."""
    chunks=telegram_chunks(summary(menu,stats,people,budget),3800)
    if edit:
        first=chunks[0]+('\n\n(продолжение ниже)' if len(chunks)>1 else '')
        await safe_call(message.edit_text,first,
                        reply_markup=result_kb(menu_id,uid) if len(chunks)==1 else None)
    else:
        await safe_call(message.answer,chunks[0],
                        reply_markup=result_kb(menu_id,uid) if len(chunks)==1 else None)
    for index,piece in enumerate(chunks[1:],1):
        await safe_call(message.answer,f'🍽 Продолжение {index+1}/{len(chunks)}\n'+piece,
                        reply_markup=result_kb(menu_id,uid) if index==len(chunks)-1 else None)


@router.callback_query(F.data.startswith('menugen:'))
async def actions(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    parts=c.data.split(':');action=parts[1];uid=c.from_user.id
    if action=='ai_review' and len(parts)==3 and parts[2].isdigit():
        from llm_lab import review_selected_menu
        return await review_selected_menu(c,int(parts[2]))
    if action=='settings':
        await state.clear()
        await c.answer()
        return await open_settings_from_inline(c.message,uid)
    if action in ('people','diet','budget','calories','run'):
        if not _card_is_current(c):
            return await c.answer('Эта карточка устарела. Откройте «🍽 Меню на неделю» заново.',show_alert=True)

    if action=='diet' and len(parts)==4 and parts[2] in MODES:
        selected=parts[2]
        await c.answer()
        save_diet_mode(uid,selected)
        return await refresh_menu_card(c.message,state,uid=uid)
    if action=='people' and len(parts)==4 and parts[2] in ('1','2','3'):
        people=int(parts[2])
        await c.answer()
        set_people(uid,people)
        return await refresh_menu_card(c.message,state,uid=uid)
    if action in ('pathbudget','pathmeat','pathpeople'):
        # Кнопки путей выхода несут отметку настроек, при которых были предложены. Устаревшую кнопку отклоняем:
        # она не должна перезаписывать более новые настройки пользователя.
        expected_len=4 if action=='pathbudget' else 3
        token=parts[-1] if len(parts)==expected_len else ''
        if token!=_current_token(uid):
            return await c.answer('Предложение устарело: настройки уже изменились. Откройте «🍽 Меню на неделю» и повторите.',show_alert=True)
        if action=='pathbudget':
            if not parts[2].isdigit():
                return await c.answer('Недопустимая сумма бюджета',show_alert=True)
            value=int(parts[2])
            if not 100<=value<=1000000:
                return await c.answer('Недопустимая сумма бюджета',show_alert=True)
            set_budget(uid,float(value))
            await c.answer(f'Бюджет изменён на {value} ₽. Нажмите «Составить меню».')
        elif action=='pathmeat':
            menu_library.option_set(uid,'meat_cap',MEAT_CAP_DEFAULT)
            await c.answer('Режим «меньше мяса» включён на одно следующее меню. Нажмите «Составить меню».')
        else:
            people_now=max(1,int(get_people(uid))-1)
            set_people(uid,people_now)
            await c.answer(f'Число человек: {people_now}. Нажмите «Составить меню».')
        return await refresh_menu_card(c.message,state,uid=uid)
    if action=='askbudget':
        # Emitted on error messages that are not the settings card: no revision to check.
        await state.set_state(MenuWizard.budget)
        await c.answer()
        return await safe_call(c.message.answer,'💰 Введите недельный бюджет в рублях (100–1 000 000).')
    if action=='budget':
        await state.update_data(menu_card_id=c.message.message_id,menu_chat_id=c.message.chat.id)
        await state.set_state(MenuWizard.budget)
        await c.answer()
        return await safe_call(c.message.answer,'💰 Введите недельный бюджет в рублях (100–1 000 000).')
    if action=='calories':
        await state.update_data(menu_card_id=c.message.message_id,menu_chat_id=c.message.chat.id)
        await state.set_state(MenuWizard.calories)
        await c.answer()
        return await safe_call(c.message.answer,'🔥 Введите калорийность на одного человека в сутки (500–5000 ккал). Например: 2000')
    if action=='reprice' and len(parts)==3 and parts[2].isdigit():
        try:
            result=await asyncio.to_thread(_reprice_saved_menu,uid,int(parts[2]))
        except (MenuInvalid,ValueError) as exc:
            return await c.answer('Не удалось проверить состав по текущим ограничениям: '+str(exc)[:120],show_alert=True)
        except Exception:
            log.exception('Price refresh of menu %s failed',parts[2])
            return await c.answer('Не удалось сохранить новый снимок цен',show_alert=True)
        if result is None:return await c.answer('Меню не найдено',show_alert=True)
        fresh_id,new_stats,old_budget,over_limit=result
        await c.answer('Новая смета создана; исходное меню сохранено')
        return await _show_long(c.message,'🔁 Новая смета (прежний снимок сохранён).\n'+
            ('⚠️ По новым ценам закупка превышает бюджет более чем на 5% или не хватает цен: снимок сохранён в истории, но не назначен «последним меню».\n' if over_limit else '')+
            'Цены ориентировочные, регион и фасовки могут быть неизвестны.\n\n'+
            estimate_text(new_stats,old_budget),
            InlineKeyboardMarkup(inline_keyboard=[
                [styled('📖 Открыть обновлённое меню',f'menugen:summary:{fresh_id}')],
                [styled('❌ Закрыть','ui:close','danger')]]),edit=False)
    if action=='latest':
        row=menu_library.latest(uid,_parameters_key(uid))
        if not row:return await c.answer('Для этих параметров меню ещё не создано.',show_alert=True)
        saved=json.loads(row['result_json'])
        await c.answer()
        return await _display_summary(c.message,row['id'],saved['menu'],saved['stats'],row['people_count'],row['budget'],edit=False,uid=uid)
    if action=='favorites':
        favorites=menu_library.favorite_list(uid)
        rows=[[styled(f"❤️ {r['title']} · {r['people_count']} чел. · {r['budget']:g} ₽",f"menugen:fopen:{r['id']}")]
              for r in favorites[:25]]
        rows.append([styled('🕘 История меню','menugen:history')])
        rows.append([styled('⬅️⬅️ К меню','menugen:backsettings'),styled('❌ Закрыть','ui:close','danger')])
        await c.answer()
        return await safe_call(c.message.answer,('❤️ Любимые меню' if favorites else '❤️ Любимых меню пока нет.'),
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    if action=='history':
        rows=menu_library.history(uid)
        buttons=[[styled(f"🕘 {r['created_at'][:16]} · {r['people_count']} чел. · {r['budget']:g} ₽",f"menugen:historic:{r['id']}")]
                 for r in rows]
        buttons.append([styled('❌ Закрыть','ui:close','danger')])
        await c.answer()
        return await safe_call(c.message.answer,
            '🕘 История меню; цены могли измениться.' if rows else 'История меню пуста.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    if action=='historic' and len(parts)==3 and parts[2].isdigit():
        with connect_db() as db:
            row=db.execute('SELECT * FROM menus WHERE id=? AND owner_id=?',(int(parts[2]),uid)).fetchone()
        if not row:return await c.answer('Меню не найдено',show_alert=True)
        saved=json.loads(row['result_json']);await c.answer()
        return await _show_long(c.message,
            '🕘 Сохранено: '+str(row['created_at'])[:10]+' · цены могли измениться.\n\n'+
            summary(saved['menu'],saved['stats'],row['people_count'],row['budget']),
            result_kb(row['id'],uid))
    if action=='favrename' and len(parts)==3 and parts[2].isdigit():
        row=menu_library.favorite_get(uid,int(parts[2]))
        if not row:return await c.answer('Любимое меню не найдено',show_alert=True)
        await state.update_data(rename_favorite_id=int(parts[2]))
        await state.set_state(MenuWizard.favorite_title)
        await c.answer()
        return await safe_call(c.message.answer,'✏️ Отправьте название любимого меню (до 90 символов).')
    if action=='backsettings':
        snapshot=_card_snapshot(uid)
        rev=menu_library.next_card(uid,c.message.chat.id)
        await c.answer()
        body,markup=_card_parts(snapshot,rev)
        card=await safe_call(c.message.answer,body,reply_markup=markup)
        if card:menu_library.card_bind(uid,c.message.chat.id,rev,card.message_id)
        return
    if action=='fopen' and len(parts)==3 and parts[2].isdigit():
        row=menu_library.favorite_get(uid,int(parts[2]))
        if not row:return await c.answer('Любимое меню не найдено.',show_alert=True)
        saved=json.loads(row['result_json']);await c.answer()
        return await _show_long(c.message,
            '❤️ '+row['favorite_title']+'\nЦены рассчитаны на '+str(row['created_at'])[:10]+', могли измениться.\n\n'+
            summary(saved['menu'],saved['stats'],row['people_count'],row['budget']),
            InlineKeyboardMarkup(inline_keyboard=result_kb(row['id'],uid).inline_keyboard[:-1]+
              [[styled('✏️ Переименовать',f"menugen:favrename:{row['favorite_id']}")],
               [styled('🗑 Убрать из любимых',f"menugen:favdelask:{row['favorite_id']}",'danger')],
               [styled('❌ Закрыть','ui:close','danger')]]))
    if action=='favadd' and len(parts)==3 and parts[2].isdigit():
        try:
            is_favorite=menu_library.favorite_toggle(uid,int(parts[2]),'Меню · '+str(_last_budget(uid) or 0)+' ₽')
        except ValueError as exc:return await c.answer(str(exc),show_alert=True)
        await c.answer()
        # Update only the keyboard; the historic menu snapshot is unchanged.
        return await safe_call(c.message.edit_reply_markup,reply_markup=result_kb(int(parts[2]),uid))
    if action=='favdelask' and len(parts)==3 and parts[2].isdigit():
        await c.answer()
        return await safe_call(c.message.answer,'Удалить это меню из любимых? Само меню останется в истории.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [styled('🗑 Удалить',f'menugen:favdel:{parts[2]}','danger')],
                [styled('❌ Отмена','ui:close')]]))
    if action=='favdel' and len(parts)==3 and parts[2].isdigit():
        menu_library.favorite_remove(uid,int(parts[2]));await c.answer('Удалено из любимых')
        return await safe_call(c.message.edit_text,'Меню удалено из любимых; исходный расчёт сохранён.')
    if action=='info':
        await c.answer()
        return await safe_call(c.message.answer,
            'ℹ️ О расчётах Жорика\n'
            'Цены — плановые ориентиры, а не рыночная средняя.\n'
            'Стоимость: по расходу всех продуктов, включая яйца, молочное и хлеб; реальные упаковки и сумма на кассе могут отличаться.\n'
            'Калории и БЖУ — справочные расчёты, не данные этикетки.\n'
            'Состав и аллергенность проверяйте по фактическим продуктам.\n'
            'Для безглютенового питания требуется отдельная проверка маркировки.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [styled('❌ Закрыть','ui:close','danger')]]))
    if action=='cancel':
        requested=parts[2] if len(parts)==3 else None
        status=cancel_generation(uid,requested) if requested is not None else 'stale'
        if status=='stale':return await c.answer('Эта генерация уже завершена',show_alert=True)
        if status=='saving':return await c.answer('Сохранение уже началось; дождитесь результата',show_alert=True)
        await c.answer()
        return await safe_call(c.message.edit_text,'Генерация отменена. Меню не сохранено.',reply_markup=error_kb())
    if action in ('run','again'):
        snapshot=_card_snapshot(uid)
        budget=snapshot['budget']
        people=snapshot['people']
        diet_mode=snapshot['diet']
        if not budget:
            await state.update_data(menu_card_id=c.message.message_id,menu_chat_id=c.message.chat.id)
            await state.set_state(MenuWizard.budget)
            await c.answer()
            return await safe_call(c.message.answer,'💰 Сначала задайте недельный бюджет в рублях.')
        if uid in _runs:return await c.answer('Генерация уже идёт',show_alert=True)
        cal=snapshot['cal']
        if not cal or cal<500 or cal>5000:return await c.answer('Нажмите «🔥 Калорийность» и укажите цель в ккал.',show_alert=True)
        op={'id':secrets.token_hex(6),'task':None,'cancelled':False,'phase':'starting','progress':None}
        # No await between the active-operation check and reservation.
        _runs[uid]=op
        try:
            await c.answer()
            progress=await c.message.answer('Жорик составляет меню…',reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[styled('❌ Отмена',f'menugen:cancel:{op["id"]}','danger')]]))
            if progress is None:raise RuntimeError('Telegram did not return a progress message')
            op['progress']=progress
        except Exception:
            if _runs.get(uid) is op:_runs.pop(uid,None)
            log.exception('Could not send menu generation progress card')
            return
        if op['cancelled'] or _runs.get(uid) is not op:
            await safe_call(progress.edit_text,'Генерация отменена.',reply_markup=error_kb())
            return
        async def job():
            from perf_diagnostics import begin,end,mark
            from perf_diagnostics import enabled as perf_enabled
            from config import settings
            from menu_workers import run_heavy,WorkersBusy
            trace=begin('menu_generation_background')
            started_total=time.perf_counter()
            stage_times={}
            def measured(label,started):
                elapsed=time.perf_counter()-started
                stage_times[label]=stage_times.get(label,0)+elapsed
                mark('menu_'+label,elapsed)
            failed=False
            menu_id=None
            deadline=time.monotonic()+MENU_GENERATION_TIMEOUT_S
            meat_cap=None;meat_raw=None;mode=None;settings_token=''
            settings_revision=None
            try:
                settings_token=await asyncio.to_thread(_current_token,uid)
                settings_revision=await asyncio.to_thread(menu_library.settings_rev,uid)
                meat_raw=await asyncio.to_thread(menu_library.option_get,uid,'meat_cap')
                meat_cap=menu_library.option_parse_int(meat_raw)
                op['phase']='building'
                step_started=time.perf_counter()
                mode=get_mode()
                if mode=='local':
                    menu,stats,prepared_context=await _within_generation_deadline(
                        local_build(uid,people,budget,cal,variation=menu_variation(uid),diet_mode=diet_mode,meat_cap=meat_cap),deadline)
                    products,prefs,prices,_=prepared_context
                else:
                    menu,stats=await _within_generation_deadline(
                        llm_build(uid,people,budget,cal,provider=mode,diet_mode=diet_mode),deadline)
                    products,prefs,prices,_=await _within_generation_deadline(run_heavy(context,uid),deadline)
                mark('menu_generation_build',time.perf_counter()-step_started)
                stage_times['build']=time.perf_counter()-step_started
                # Offload enrichment, packaging estimates and validation so
                # other users' callback handlers remain responsive.
                step_started=time.perf_counter()
                menu,stats=await _within_generation_deadline(run_heavy(
                    _finalize_generated,menu,stats,products,prefs,prices,
                    people,budget,cal,diet_mode),deadline)
                mark('menu_generation_finalize',time.perf_counter()-step_started)
                stage_times['finalize']=time.perf_counter()-step_started
                # Q1.1: editor is optional and owner-triggered through the lab.
                # Local validation above is mandatory and authoritative.
                # Debug timing must not turn paid Qwen calls back on.
                review_calls=0
                review_status='skipped_policy'
                issues=[]
                stats['qwen_review_status']=review_status
                stats['qwen_review_issues']=len(issues)
                if time.monotonic()>=deadline:raise asyncio.TimeoutError
                if op['cancelled'] or _runs.get(uid) is not op:raise asyncio.CancelledError
                if await asyncio.to_thread(_current_token,uid)!=settings_token:
                    raise MenuInvalid('Настройки питания изменились во время генерации. '
                                      'Меню не сохранено; повторите генерацию.')
                step_started=time.perf_counter()
                # Commit boundary: cancellation after this point is refused.
                # The shielded worker owns a fresh SQLite connection and finishes
                # its transaction even if Telegram delivery subsequently fails.
                op['phase']='saving'
                save_task=asyncio.create_task(run_heavy(_save_generated,uid,people,budget,
                    cal,diet_mode,prefs,menu,stats,settings_revision))
                op['save_task']=save_task
                _pending_saves.add(save_task)
                save_task.add_done_callback(lambda task:_observe_save(op,task))
                menu_id=await asyncio.shield(save_task)
                if meat_cap is not None and mode=='local':  # режим действует на одно меню и снимается, только если был применён (внешний ИИ его не поддерживает); выбранную во время генерации настройку не трогаем
                    menu_library.option_clear_if(uid,'meat_cap',meat_raw)
                mark('menu_generation_save',time.perf_counter()-step_started)
                stage_times['save']=time.perf_counter()-step_started
                step_started=time.perf_counter()
                await _display_summary(progress,menu_id,menu,stats,people,budget,uid=uid)
                stage_times['telegram_summary']=time.perf_counter()-step_started
                # Owner debug mode: exact elapsed timings, no user/menu text or PII.
                # PERF05 is switched on/off in the existing admin panel.
                if perf_enabled() and uid==settings.sandbox_owner_id:
                    total=time.perf_counter()-started_total
                    known=sum(stage_times.values())
                    lines=['🔧 Жорик · время генерации (только владелец)',
                           f'Всего: {total:.2f} с',
                           *[f'{label}: {value:.2f} с' for label,value in stage_times.items()],
                           f'Прочие операции/ожидания: {max(0,total-known):.2f} с',
                           f'Обращений к Qwen: {review_calls}; статус: {review_status}; замечаний: {len(issues)}',
                           'Время SQLite и Telegram пооперационно — в файле PERF05.',
                           'Число токенов Qwen: не измерено в этой карточке.']
                    await safe_call(progress.answer,'\n'.join(lines),reply_markup=error_kb())
            except asyncio.CancelledError:
                failed=True
                if op['phase']=='saving':
                    op['orphaned']=True
                    if op.get('save_task') and op['save_task'].done():
                        _observe_save(op,op['save_task'])
                else:
                    await safe_call(progress.edit_text,'Генерация отменена. Меню не сохранено.',reply_markup=error_kb())
                raise
            except asyncio.TimeoutError:
                failed=True
                log.warning('Menu generation timed out after %ss',MENU_GENERATION_TIMEOUT_S)
                await safe_call(progress.edit_text,
                    '⏱ Не удалось закончить меню вовремя. Генерация остановлена, меню не сохранено. '
                    'Попробуйте ещё раз или измените условия.',
                    reply_markup=error_kb())
            except WorkersBusy:
                failed=True
                await safe_call(progress.edit_text,'⏳ Предыдущая обработка ещё завершается. Попробуйте чуть позже.',reply_markup=error_kb())
            except sqlite3.OperationalError as exc:
                failed=True
                log.exception('Menu SQLite save failed')
                text=('⏳ База данных занята другой записью. Меню не сохранено; повторите попытку позже.'
                      if 'locked' in str(exc).lower() else
                      '⚠️ Не удалось записать меню в базу. Проверьте журнал ошибок.')
                await safe_call(progress.edit_text,text,reply_markup=error_kb())
            except MenuInvalid as exc:
                failed=True
                log.warning('Menu generation rejected: %s', str(exc)[:350].replace('\n', ' '))
                # Only local, controlled calorie feasibility message is safe to display.
                error=str(exc)
                if error.startswith('Неизвестная группа или продукт-аллерген:'):
                    message=(error[:180]+'\n\nЖорик пока распознаёт только отдельные группы: '
                             'молоко, яйца, арахис, орехи, рыба, пшеница, глютен; '
                             'а также лук и мёд. Для безопасности меню не создано. '
                             'Уточните аллерген в настройках.')
                elif error.startswith('Настройки питания изменились во время генерации.'):
                    message=error
                elif error.startswith('Бюджет ') or isinstance(exc,BudgetExceeded):
                    needed=getattr(exc,'needed',None)
                    limit=budget*menu_rules.BUDGET_FACTOR
                    tol=menu_rules.BUDGET_TOLERANCE*100
                    paths=[];hints=[]
                    if needed and math.isfinite(needed):
                        raised=menu_rules.suggested_budget(needed)
                        if budget<raised<=1000000:
                            paths.append((f'💰 Поднять бюджет до {raised} ₽',f'menugen:pathbudget:{raised}:{settings_token}'))
                            hints.append(f'поднять бюджет до {raised} ₽')
                    if meat_cap is None and (mode or 'local')=='local':  # внешний ИИ-режим ограничение мяса не поддерживает
                        paths.append((f'🥦 Меньше мяса (до {MEAT_CAP_DEFAULT} приёмов в неделю)',f'menugen:pathmeat:{settings_token}'))
                        hints.append(f'меньше мяса: не более {MEAT_CAP_DEFAULT} приёмов в неделю')
                    if people>1:
                        paths.append((f'👥 Уменьшить до {people-1} чел.',f'menugen:pathpeople:{settings_token}'))
                        hints.append(f'уменьшить число человек до {people-1}')
                    message=(f'💰 Меню на {people} чел. · бюджет {budget:g} ₽ · {cal:g} ккал/чел.\n'
                             +(f'Подобрать меню в бюджет не удалось: ближайший найденный вариант стоит около {needed:.0f} ₽ '
                                f'(по расходу продуктов без округления до упаковок; допуск +{tol:.0f}%, то есть до {limit:.0f} ₽).\n' if needed else
                                f'Подобрать меню в бюджет (допуск +{tol:.0f}%) не удалось.\n')
                             +'Цены ориентировочные: в вашем магазине они могут быть выгоднее. Меню не сохранено.'
                             +(('\n\nКак выйти:\n'+'\n'.join(f'{i}) {h}' for i,h in enumerate(hints,1))+'\nВыберите путь, затем нажмите «Составить меню» на карточке настроек. Предложения действуют, пока настройки не менялись.') if hints else ''))
                    await safe_call(progress.edit_text,message,reply_markup=error_kb(change_budget=True,paths=paths))
                    return
                elif error.startswith('Нет цен для полной проверки закупки:'):
                    message='⚠️ Не хватает данных о ценах или фасовках для подтверждения закупки. '+error
                elif (error.startswith('Цель ') and 'недостижима' in error) or error.startswith('Нет рецептов с известной калорийностью'):
                    message=error
                elif error.startswith('Блюдо не прошло проверку выбранного типа питания:'):
                    message=('Для выбранного режима пока не удалось подобрать проверяемое меню. '
                             'Измените тип питания или условия. '+GLUTEN_NOTICE if diet_mode=='gluten_avoid' else error)
                else:
                    message='Не удалось сформировать меню. Проверьте каталог, ограничения и настройки выбранного режима.'
                await safe_call(progress.edit_text,message,reply_markup=error_kb())
            except Exception:
                failed=True
                if menu_id is not None:
                    log.exception('Menu %s committed but Telegram delivery failed',menu_id)
                    text=f'✅ Меню №{menu_id} сохранено, но Telegram не смог обновить карточку. Откройте историю меню.'
                else:
                    log.exception('Menu generation failed for mode=%s',get_mode())
                    text='Не удалось сформировать меню. Проверьте каталог, ограничения и настройки выбранного режима.'
                try:await safe_call(progress.edit_text,text,reply_markup=error_kb())
                except Exception:log.warning('Cannot update failed generation card',exc_info=True)
            finally:
                end(trace,failed)
                if _tasks.get(uid) is asyncio.current_task():
                    _tasks.pop(uid,None)
                if _runs.get(uid) is op:_runs.pop(uid,None)
        task=asyncio.create_task(job())
        op['task']=task
        _tasks[uid]=task
        return
    if action=='export' and len(parts)==4 and parts[2].isdigit() and parts[3] in ('txt','docx'):
        menu_id=int(parts[2]);fmt=parts[3]
        with connect_db() as db:
            row=db.execute('SELECT people_count,budget,calorie_target,result_json FROM menus WHERE id=? AND owner_id=?',(menu_id,uid)).fetchone()
        if not row:return await c.answer('Меню не найдено',show_alert=True)
        saved=json.loads(row['result_json'])
        export_intro=summary(saved['menu'],saved['stats'],row['people_count'],row['budget']).replace('Граммовки и калории блюд — кнопка «📅 По дням и граммовкам».\n','')
        content='\n\n'.join([export_intro]+[day_text(saved['menu'],i,row['people_count'],row['calorie_target'],saved['stats']) for i in range(7)]+[estimate_text(saved['stats'],row['budget'])])
        if fmt=='txt':payload=content.encode('utf-8-sig')
        else:
            from docx import Document
            from io import BytesIO
            document=Document()
            for i,section in enumerate(content.split('\n\n')):
                for line in section.splitlines():document.add_paragraph(line)
                if i<8:document.add_paragraph('')
            stream=BytesIO();document.save(stream);payload=stream.getvalue()
        await c.answer()
        return await safe_call(c.message.answer_document,BufferedInputFile(payload,filename=f'zhorik_menu_{menu_id}.{fmt}'),caption='🍽 Полное меню на неделю')
    if action in ('day','estimate','summary') and len(parts)>=3 and parts[2].isdigit():
        menu_id=int(parts[2])
        with connect_db() as db:
            row=db.execute('SELECT people_count,budget,calorie_target,result_json FROM menus WHERE id=? AND owner_id=?',(menu_id,uid)).fetchone()
        if not row:return await c.answer('Меню не найдено',show_alert=True)
        saved=json.loads(row['result_json'])
        if action=='estimate':
            await c.answer()
            return await _show_long(c.message,estimate_text(saved['stats'],row['budget']),
                InlineKeyboardMarkup(inline_keyboard=[[styled('❌ Закрыть','ui:close','danger')]]),edit=False)
        if action=='summary':
            await c.answer()
            return await _display_summary(c.message,menu_id,saved['menu'],saved['stats'],row['people_count'],row['budget'],uid=uid)
        if len(parts)!=4 or not parts[3].isdigit() or int(parts[3])>6:return await c.answer('Некорректный день',show_alert=True)
        idx=int(parts[3]);await c.answer()
        chunks=telegram_chunks(day_text(saved['menu'],idx,row['people_count'],row['calorie_target'],saved['stats']))
        if len(chunks)==1:
            return await safe_call(c.message.edit_text,chunks[0],reply_markup=day_kb(menu_id,idx))
        await safe_call(c.message.edit_text,chunks[0]+'\n\n(продолжение следующим сообщением)')
        for index,part in enumerate(chunks[1:],1):
            last=index==len(chunks)-1
            await safe_call(c.message.answer,
                            f'📅 Продолжение {index+1}/{len(chunks)}\n'+part,
                            reply_markup=day_kb(menu_id,idx) if last else None)
        return
    if action=='list' and len(parts)==3 and parts[2].isdigit():
        menu_id=int(parts[2])
        with connect_db() as db:
            record=db.execute('SELECT result_json FROM menus WHERE id=? AND owner_id=?',(menu_id,uid)).fetchone()
        if not record:return await c.answer('Меню не найдено',show_alert=True)
        ingredients=json.loads(record['result_json'])['stats']['ingredients']
        preview=['🛒 Ингредиенты для недельного меню',
                 'Это суммарный расход для всех участников, а не число упаковок к покупке. '
                 'Смета также считается по расходу: фактическую фасовку и сумму покупки уточняйте в магазине.']
        for key,amount in ingredients:
            preview.append(f'• {key[0]} — {amount:g} {key[1]}')
        preview.append('Список будет сохранён отдельно. Существующие списки не изменятся.')
        await c.answer()
        return await _show_long(c.message,'\n'.join(preview),InlineKeyboardMarkup(inline_keyboard=[
            [styled('💾 Сохранить в мои списки',f'menugen:listsave:{menu_id}','success')],
            [styled('❌ Закрыть','ui:close','danger')]]),edit=False)
    if action=='listsave' and len(parts)==3 and parts[2].isdigit():
        menu_id=int(parts[2])
        try:
            list_id=await asyncio.to_thread(_save_menu_shopping_list,uid,menu_id)
            page=await asyncio.to_thread(get_page,uid,list_id) if list_id is not None else None
        except Exception:
            log.exception('Could not save shopping list for menu %s',menu_id)
            return await c.answer('Не удалось сохранить список покупок. Попробуйте позже.',show_alert=True)
        if list_id is None:return await c.answer('Меню не найдено',show_alert=True)
        await safe_call(c.message.edit_text,page_text(page),reply_markup=paged_list_keyboard(list_id,page))
        return await c.answer()
    await c.answer()

