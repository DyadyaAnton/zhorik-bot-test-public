"""Read-only entry point for the future verified KBJU reference library."""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from access import check_callback, check_message
from keyboards import styled
from telegram_retry import safe_call
from nutrition_library import count, find_products
from price_b_catalog import seeds
from calorie_catalog import energy
from nutrition_reference import kbju_for
from nutrition_candidates import candidate_for

router = Router(name='nutrition_ui')

def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('🥦 Продукты и КБЖУ · 258', 'nutrition:groups')],
        [styled('📊 Проверенные КБЖУ', 'nutrition:products')],
        [styled('🥘 Рецепты', 'nutrition:recipes'),styled('📅 Готовые меню','nutrition:plans')],
        [styled('❌ Закрыть','ui:close','danger')],
    ])

def page_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('⬅️⬅️ Назад','nutrition:home','success')],
        [styled('❌ Закрыть','ui:close','danger')],
    ])

def intro():
    totals=count()
    reference_count=sum(1 for _,name,_,_ in seeds() if kbju_for(name) is not None)
    candidate_count=sum(1 for _,name,_,_ in seeds() if kbju_for(name) is None and candidate_for(name) is not None)
    return ('📚 Библиотека питания Жорика\n'
            f'Продуктов: {totals["products"]} · рецептов: {totals["recipes"]} · готовых меню: {totals["plans"]}\n\n'
            f'В каталоге из 258 продуктов есть {reference_count} справочных КБЖУ; '
            f'ещё {candidate_count} совпадений с неподтверждённой таблицей.\n'
            'Кандидаты КБЖУ показаны отдельно и не используются в расчёте рационов.\n'
            'Справочник КБЖУ хранится отдельно от цен. Пока загруженные PDF проходят сверку, '
            'непроверенные значения не публикуются как точные.\n\n'
            'Для предварительного каталога 258 продуктов нажмите «Продукты и КБЖУ».\n'
            '⚠️ Справочные данные, не медицинская рекомендация.')


def _groups():
    result={}
    for row in seeds():result.setdefault(row[2],[]).append(row)
    return sorted(result.items(),key=lambda x:x[0])


def _group_list():
    kb=[]
    for index,(name,rows) in enumerate(_groups()):
        kb.append([styled(f'{name} · {len(rows)}',f'nutrition:g:{index}:0')])
    kb.append([styled('⬅️⬅️ Назад','nutrition:home','primary'),styled('✖️ Закрыть','ui:close','danger')])
    return '🥦 Продукты и КБЖУ · 258\nВыберите группу. Данные на 100 г или обозначено «нет данных».',InlineKeyboardMarkup(inline_keyboard=kb)


def _group_page(index,offset):
    name,rows=_groups()[index]
    if not 0<=offset<len(rows):raise ValueError('Некорректная страница')
    kb=[]
    for code,title,_,unit in rows[offset:offset+5]:
        kcal=energy(title,100)
        if kcal is None and (reference:=kbju_for(title)):kcal=reference[0]
        kb.append([styled(f'{title[:24]} · {kcal:g} ккал' if kcal is not None else title[:31],
                          f'nutrition:item:{index}:{offset}:{code}')])
    nav=[]
    if offset>0:nav.append(styled('⬅️⬅️',f'nutrition:g:{index}:{max(0,offset-5)}'))
    if offset+5<len(rows):nav.append(styled('➡️➡️',f'nutrition:g:{index}:{offset+5}'))
    if nav:kb.append(nav)
    kb.append([styled('⬅️⬅️ Группы','nutrition:groups','primary'),styled('✖️ Закрыть','ui:close','danger')])
    return f'🥦 {name}\nПродукты {offset+1}–{min(offset+5,len(rows))} / {len(rows)}\nКкал — предварительные справочные оценки.',InlineKeyboardMarkup(inline_keyboard=kb)


def _product_page(index,offset,code):
    rows=_groups()[index][1]
    item=next((r for r in rows if r[0]==code),None)
    if item is None:raise ValueError('Продукт не найден')
    _,name,category,unit=item
    ref=kbju_for(name)
    tentative=candidate_for(name) if ref is None else None
    kcal=energy(name,100)
    if ref:
        data=f'Калорийность: {ref[0]:g} ккал\nБелки: {ref[1]:g} г · Жиры: {ref[2]:g} г · Углеводы: {ref[3]:g} г'
        source='Таблица калорийности из присланных материалов; справочные значения, не этикетка.'
    elif tentative:
        _,(tk,tp,tf,tc),confidence=tentative
        data=(f'Кандидат (ещё не проверен): {tk:g} ккал\n'
              f'Белки: {tp:g} г · Жиры: {tf:g} г · Углеводы: {tc:g} г')
        source=('Неподтверждённая таблица КБЖУ, класс '+confidence+
                '. Возможны отличия сортов, фасовок и приготовления. Не используется для сметы/меню.')
    elif kcal is not None:
        data=f'Калорийность: ≈ {kcal:g} ккал\nБЖУ: данных пока нет.'
        source='Условная оценка калорийности без проверки этикетки.'
    else:
        data='Калорийность и БЖУ: надёжных данных пока нет.'
        source='Значения не выдумываются.'
    text=f'🥦 {name}\nКод: {code}\nГруппа: {category}\n\nНа 100 г продукта:\n{data}\n\n⚠️ {source}'
    return text,InlineKeyboardMarkup(inline_keyboard=[
        [styled('⬅️⬅️ К списку',f'nutrition:g:{index}:{offset}','primary'),styled('✖️ Закрыть','ui:close','danger')]])

@router.message(Command('calories'))
async def command(m:Message):
    if not await check_message(m):return
    await safe_call(m.answer,intro(),reply_markup=keyboard())

@router.callback_query(F.data.startswith('nutrition:'))
async def callback(c:CallbackQuery):
    if not await check_callback(c):return
    action=c.data.split(':',1)[1]
    if action=='home':
        message=intro();markup=keyboard()
    elif action=='groups':
        message,markup=_group_list()
    elif action.startswith('g:') or action.startswith('item:'):
        try:
            parts=action.split(':')
            if parts[0]=='g' and len(parts)==3:
                message,markup=_group_page(int(parts[1]),int(parts[2]))
            elif parts[0]=='item' and len(parts)==4:
                message,markup=_product_page(int(parts[1]),int(parts[2]),parts[3])
            else:raise ValueError('Некорректная кнопка')
        except (IndexError,ValueError):
            return await c.answer('Обновите справочник через /calories',show_alert=True)
    elif action=='products':
        rows=[r for r in find_products() if r["confidence"] == "verified"]
        message='📊 КБЖУ продуктов на 100 г\n\n' + ('\n'.join(f'• {r["canonical_name"]} ({r["preparation"]}): {r["kcal_100g"]} ккал · Б {r["protein_100g"]} · Ж {r["fat_100g"]} · У {r["carbs_100g"]}' for r in rows) if rows else 'Проверенные данные ещё не загружены.')
        markup=page_keyboard()
    elif action=='recipes':
        message='🥘 Библиотека рецептов\n\nИмпорт рецептов из PDF готовится отдельно. Пока проверенных карточек нет.';markup=page_keyboard()
    elif action=='plans':
        message='📅 Примеры готовых меню\n\nЗагруженные документы пока на проверке. Не выдаём неполные рационы за проверенные.';markup=page_keyboard()
    else:return await c.answer('Неизвестное действие',show_alert=True)
    await safe_call(c.message.edit_text,message,reply_markup=markup)
    await c.answer()
