"""User recipe browser: categories, details and durable favorites."""
from __future__ import annotations

import json
import asyncio
from functools import lru_cache
from io import BytesIO
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from access import check_message, check_callback
from database import connect_db
from keyboards import styled
from recipe_bank import export_json, import_json, rollback_import_batch
from config import settings
from menu_diet import recipe_is_eligible, DISCLAIMER as GLUTEN_NOTICE
from recipe_labels import display_title
from nutrition_reference import kbju_for
from calorie_catalog import energy
import porridge_builder
import dish_builder

router = Router(name="recipe_ui")
PAGE = 5
_awaiting_recipe_json=set()


def _ru_key(value):
    """Stable Russian-friendly ordering with ё beside е; no locale dependency."""
    return (str(value).casefold().replace('ё','е'), str(value).casefold())


def _recipe_nutrition(ingredients):
    """Separate partial calories and complete macros; never substitute absent values with zero."""
    kcal=0.0; macros=[0.0,0.0,0.0]; full_kcal=True; full_macros=True
    for item in ingredients:
        name=item['product']; unit=item['unit']; quantity=float(item['amount'])
        if unit=='кг':grams=quantity*1000
        elif unit=='г':grams=quantity
        else:
            full_kcal=False;full_macros=False
            continue
        val=energy(name,grams)
        if val is None:full_kcal=False
        else:kcal+=val
        ref=kbju_for(name)
        if ref is None:full_macros=False
        else:
            for i in range(3): macros[i]+=float(ref[i+1])*grams/100
    return (round(kcal), tuple(round(x,1) for x in macros) if full_macros else None, full_kcal)


def _categories():
    with connect_db() as db:
        return sorted(db.execute("SELECT category, COUNT(*) FROM zr_recipes WHERE active=1 GROUP BY category").fetchall(), key=lambda r:_ru_key(r[0]))


def _favorite_count(uid):
    with connect_db() as db:
        return db.execute("""SELECT COUNT(*) FROM zr_favorites f JOIN zr_recipes r
            ON r.id=f.recipe_id WHERE f.user_id=?""", (uid,)).fetchone()[0]


def _recipe_basis(category, title, ingredients):
    """Stable browsing labels; no schema changes or inferred product prices."""
    names = [i['product'].casefold().replace('ё','е') for i in ingredients]
    t = title.casefold().replace('ё','е')
    if category == 'Каши':
        for token, label in [('овсян','Овсянка'),('греч','Гречка'),('рис','Рис'),
                             ('пшен','Пшено'),('манн','Манка')]:
            if token in t:return label
    if category == 'Супы':
        for token, label in [('минтай','Минтай'),('горбуш','Горбуша'),('треск','Треска'),
                             ('рыб','Рыбные'),('куриц','Куриные'),('говядин','Говядина'),
                             ('свинин','Свинина'),('капуст','Капуста'),('картоф','Картофель'),
                             ('рис','Рис'),('греч','Гречка')]:
            if token in t:return label
    groups = {
        'Из мяса': [('говядин','Говядина'),('свинин','Свинина')],
        'Из курицы и индейки': [('индей','Индейка'),('куриц','Курица')],
        'Рыба': [('минтай','Минтай'),('горбуш','Горбуша'),('треск','Треска'),
                 ('хек','Хек'),('скумбр','Скумбрия')],
        'Из яиц': [('омлет','Омлет'),('яичниц','Яичница'),('яйц','Яйца')],
        'Торты и десерты': [('торт','Торты'),('пирог','Пироги'),
                            ('запеканк','Запеканки'),('десерт','Десерты')],
    }
    for token, label in groups.get(category, []):
        if token in t:return label
    for name in names:
        if name in t:return name.capitalize()
    return 'Другие'


def _dish_type(category, title):
    """Presentation-only dish classification; recipe IDs and legacy data stay intact."""
    t = title.casefold().replace('ё', 'е')
    if category == 'Супы':return 'Супы'
    if category == 'Салаты':return 'Салаты'
    if category == 'Торты и десерты':return 'Десерты и выпечка'
    if category == 'Бутерброды и закуски':return 'Закуски и бутерброды'
    if category in ('Каши', 'Блины, сырники и оладьи', 'Из яиц'):
        return 'Завтраки'
    if 'запеканк' in t:return 'Запеканки'
    if 'котлет' in t or 'тефтел' in t or 'биточк' in t:return 'Котлеты и тефтели'
    if 'рагу' in t:return 'Рагу'
    if 'тушен' in t or 'тушён' in title.casefold():return 'Тушёные блюда'
    if 'запеч' in t or 'духовк' in t:return 'Запечённые блюда'
    if category == 'Макароны и запеканки':return 'Паста и блюда из круп'
    if category == 'Овощи и гарниры':return 'Гарниры и овощные блюда'
    return 'Вторые блюда'


def _grouped(category):
    with connect_db() as db:
        rows=db.execute("SELECT id,title,ingredients_json FROM zr_recipes WHERE active=1 AND category=? ORDER BY id",(category,)).fetchall()
    groups={}
    for row in rows:
        label=_recipe_basis(category,row['title'],json.loads(row['ingredients_json']))
        groups.setdefault(label,[]).append(row)
    return sorted(groups.items(),key=lambda x:_ru_key(x[0]))


def _groups_page(index):
    categories=_categories()
    if not 0<=index<len(categories):raise ValueError('Категория не найдена')
    category,total=categories[index]
    groups=_grouped(category)
    kb=InlineKeyboardBuilder()
    for group_index,(label,rows) in enumerate(groups):
        kb.row(styled(f'{label} · {len(rows)}',f'recipes:group:{index}:{group_index}:0'))
    kb.row(styled('⬅️⬅️ Рубрики','recipes:home','primary'),
           styled('✖️ Закрыть','recipes:close','danger'))
    return f'📚 {category} · {total} рецептов\nВыберите основу блюда:',kb.as_markup()


def _group_page(index,group_index,offset):
    category=_categories()[index][0]
    label,rows=_grouped(category)[group_index]
    total=len(rows)
    if not 0<=offset<total:raise ValueError('Страница не найдена')
    kb=InlineKeyboardBuilder()
    for row in sorted(rows,key=lambda r:(_ru_key(display_title(r['title'])),r['id']))[offset:offset+PAGE]:
        kb.row(styled(display_title(row['title'])[:32],f"recipes:gdetail:{row['id']}:{index}:{group_index}:{offset}"))
    arrows=[]
    if offset:arrows.append(styled('⬅️⬅️',f'recipes:group:{index}:{group_index}:{max(0,offset-PAGE)}'))
    if offset+PAGE<total:arrows.append(styled('➡️➡️',f'recipes:group:{index}:{group_index}:{offset+PAGE}'))
    if arrows:kb.row(*arrows)
    kb.row(styled('⬅️⬅️ Основы','recipes:groups:'+str(index),'primary'),
           styled('✖️ Закрыть','recipes:close','danger'))
    return f'📖 {category} → {label}\nРецепты {offset+1}–{min(offset+PAGE,total)} / {total}',kb.as_markup()


def _favorites_page(uid,offset):
    with connect_db() as db:
        rows=db.execute('''SELECT r.id,r.title FROM zr_favorites f JOIN zr_recipes r
            ON r.id=f.recipe_id WHERE f.user_id=? ORDER BY r.title,r.id''',(uid,)).fetchall()
    with connect_db() as db:
        variants=db.execute('SELECT id,title,kind FROM zm_recipe_variants WHERE owner_id=? ORDER BY id DESC',(uid,)).fetchall()
    total=len(rows)+len(variants)
    offset=min(max(0,offset),max(0,((total-1)//PAGE)*PAGE))
    kb=InlineKeyboardBuilder()
    combined=[('recipe',r) for r in sorted(rows,key=lambda r:(_ru_key(display_title(r['title'])),r['id']))]
    combined.extend(('variant',v) for v in variants)
    for kind,row in combined[offset:offset+PAGE]:
        if kind=='recipe':
            kb.row(styled(display_title(row['title'])[:32],f"recipes:fdetail:{row['id']}:{offset}"))
        else:
            action=('porridge:open:' if row['kind']=='porridge' else 'dish:open:')+str(row['id'])
            kb.row(styled('❤️ '+row['title'][:30],action))
    arrows=[]
    if offset:arrows.append(styled('⬅️⬅️',f'recipes:favs:{max(0,offset-PAGE)}'))
    if offset+PAGE<total:arrows.append(styled('➡️➡️',f'recipes:favs:{offset+PAGE}'))
    if arrows:kb.row(*arrows)
    kb.row(styled('⬅️⬅️ Рубрики','recipes:home','primary'),
           styled('✖️ Закрыть','recipes:close','danger'))
    return f'❤️ Любимые рецепты и блюда · {total} записей'+(f'\n{offset+1}–{min(total,offset+PAGE)} / {total}' if total else '\nПока пусто.'),kb.as_markup()


@lru_cache(maxsize=3)
def _diet_rows(mode):
    """Virtual view: do not duplicate or mutate recipe records."""
    if mode not in ('regular','pp','gluten_avoid'):raise ValueError('Режим не найден')
    with connect_db() as db:
        candidates=db.execute('''SELECT id,title,category,ingredients_json,nutrition_status
                                 FROM zr_recipes WHERE active=1 ORDER BY category,title,id''').fetchall()
    rows=[]
    for row in candidates:
        if mode=='regular':
            rows.append(row)
            continue
        parts=json.loads(row['ingredients_json'])
        if row['nutrition_status']=='unknown':continue
        if recipe_is_eligible([(x['product'],x['amount']) for x in parts],mode,
                              title=row['title'],category=row['category']):rows.append(row)
    return tuple(rows)


@lru_cache(maxsize=3)
def _diet_categories(mode):
    groups={}
    for row in _diet_rows(mode):
        groups.setdefault(_dish_type(row['category'],row['title']),[]).append(row)
    return tuple((name,tuple(sorted(rows,key=lambda r:(_ru_key(display_title(r['title'])),r['id'])))) for name,rows in sorted(groups.items(),key=lambda x:_ru_key(x[0])))


@lru_cache(maxsize=45)
def _diet_bases(mode,category_index):
    categories=_diet_categories(mode)
    if not 0<=category_index<len(categories):raise ValueError('Рубрика не найдена')
    category,rows=categories[category_index]
    groups={}
    for row in rows:
        label=_recipe_basis(row['category'],row['title'],json.loads(row['ingredients_json']))
        groups.setdefault(label,[]).append(row)
    return category,tuple((name,tuple(rows)) for name,rows in sorted(groups.items(),key=lambda x:_ru_key(x[0])))


def invalidate_recipe_views():
    """Owner import changes categories, names and their callback indices."""
    _diet_bases.cache_clear()
    _diet_categories.cache_clear()
    _diet_rows.cache_clear()


def _diet_label(mode):
    return {'regular':'🍽 Обычные рецепты',
            'pp':'🥗 ПП / облегчённые варианты',
            'gluten_avoid':'🌾 Без глютена · предварительный фильтр'}[mode]


def _virtual_page(mode):
    """First level after diet mode; legacy callbacks checked by dispatcher."""
    categories=_diet_categories(mode)
    total=sum(len(rows) for _,rows in categories)
    kb=InlineKeyboardBuilder()
    for index,(category,rows) in enumerate(categories):
        kb.row(styled(f'{category} · {len(rows)}',f'recipes:vc:{mode}:{index}'))
    kb.row(styled('⬅️⬅️ Тип питания','recipes:home','primary'),
           styled('✖️ Закрыть','recipes:close','danger'))
    note=('Это предварительный подбор, не медицинская классификация.' if mode=='pp'
          else GLUTEN_NOTICE if mode=='gluten_avoid' else 'Рецепты содержат непроверенные шаблоны.')
    return f'{_diet_label(mode)} · {total} рецептов\nВыберите рубрику.\n{note}',kb.as_markup()


def _virtual_bases(mode,category_index):
    category,groups=_diet_bases(mode,category_index)
    kb=InlineKeyboardBuilder()
    for group_index,(name,rows) in enumerate(groups):
        kb.row(styled(f'{name} · {len(rows)}',f'recipes:vg:{mode}:{category_index}:{group_index}:0'))
    kb.row(styled('⬅️⬅️ Рубрики',f'recipes:virtual:{mode}:0','primary'),
           styled('✖️ Закрыть','recipes:close','danger'))
    return f'{_diet_label(mode)} → {category}\nВыберите подгруппу рецептов:',kb.as_markup()


def _virtual_recipes(mode,category_index,group_index,offset):
    category,groups=_diet_bases(mode,category_index)
    if not 0<=group_index<len(groups):raise ValueError('Основа не найдена')
    basis,rows=groups[group_index]
    total=len(rows)
    if not 0<=offset<total:raise ValueError('Страница не найдена')
    kb=InlineKeyboardBuilder()
    for row in sorted(rows,key=lambda r:(_ru_key(display_title(r['title'])),r['id']))[offset:offset+PAGE]:
        kb.row(styled(display_title(row['title'])[:32],f"recipes:vr:{mode}:{row['id']}:{category_index}:{group_index}:{offset}"))
    nav=[]
    if offset:nav.append(styled('⬅️⬅️',f'recipes:vg:{mode}:{category_index}:{group_index}:{max(0,offset-PAGE)}'))
    if offset+PAGE<total:nav.append(styled('➡️➡️',f'recipes:vg:{mode}:{category_index}:{group_index}:{offset+PAGE}'))
    if nav:kb.row(*nav)
    kb.row(styled('⬅️⬅️ Основы',f'recipes:vc:{mode}:{category_index}','primary'),
           styled('✖️ Закрыть','recipes:close','danger'))
    return (f'{_diet_label(mode)} → {category} → {basis}\n'
            f'Рецепты {offset+1}–{min(total,offset+PAGE)} / {total}',kb.as_markup())


def _category_markup(uid=None):
    kb = InlineKeyboardBuilder()
    if uid is not None:
        kb.row(styled('❤️ Любимые рецепты и блюда','recipes:favs:0','primary'))
    kb.row(styled('🍽 Собрать блюдо','dish:home','success'))
    kb.row(styled('🍽 Обычные','recipes:virtual:regular:0'))
    kb.row(styled('🥗 ПП / облегчённые','recipes:virtual:pp:0'),
           styled('🌾 Без глютена*','recipes:virtual:gluten_avoid:0'))
    kb.row(styled('⬅️⬅️ Назад','recipes:back','primary'),
           styled('❌ Закрыть','recipes:close','danger'))
    return kb.as_markup()


def _list_page(index, offset):
    categories = _categories()
    if not 0 <= index < len(categories):
        raise ValueError("Категория не найдена")
    category, total = categories[index]
    with connect_db() as db:
        rows = db.execute("""SELECT id,title,nutrition_status FROM zr_recipes
              WHERE active=1 AND category=? ORDER BY title,id LIMIT ? OFFSET ?""",
                          (category, PAGE, offset)).fetchall()
    text = (f"📖 {category} · {offset+1}–{min(offset+PAGE,total)} / {total}\n"
            "Выберите рецепт. Данные и калории предварительные.")
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.row(styled(display_title(row["title"])[:28], f"recipes:detail:{row['id']}:{index}:{offset}"))
    arrows = []
    if offset > 0:
        arrows.append(styled("⬅️⬅️", f"recipes:cat:{index}:{max(0,offset-PAGE)}"))
    if offset+PAGE < total:
        arrows.append(styled("➡️➡️", f"recipes:cat:{index}:{offset+PAGE}"))
    if arrows:kb.row(*arrows)
    kb.row(styled("⬅️⬅️ Рубрики", "recipes:home", "primary"),
           styled("✖️ Закрыть", "recipes:close", "danger"))
    return text, kb.as_markup()


# Optional additions are previews, not new persistent recipe variants.
PORRIDGE_ADDONS={
    'banana':('Бананы',80),
    'apple':('Яблоки',80),
    'milk':('Молоко',100),
    'curd':('Творог',60),
    'butter':('Масло сливочное',10),
}


def _porridge_extras(uid,recipe_id,choice=None):
    with connect_db() as db:
        row=db.execute('SELECT title,category,ingredients_json FROM zr_recipes WHERE id=? AND active=1',(recipe_id,)).fetchone()
    if not row or row['category']!='Каши':raise ValueError('Не найдена каша')
    ingredients=json.loads(row['ingredients_json'])
    if choice is None:
        text=(f'🥣 {display_title(row["title"])}\n'
              'Добавьте один продукт для предварительного расчёта. '
              'Рецепт в базе не изменится.')
    else:
        if choice not in PORRIDGE_ADDONS:raise ValueError('Нет такой добавки')
        product,amount=PORRIDGE_ADDONS[choice]
        from menu_generator import context as _menu_context, blocked
        from menu_diet import get as _current_diet, recipe_is_eligible
        _, prefs, _, _ = _menu_context(uid)
        if blocked(product,prefs):
            raise ValueError('Добавка исключена настройками или аллергенами')
        if not recipe_is_eligible([(product,amount)],_current_diet(uid)):
            raise ValueError('Добавка не подходит для текущего режима питания')
        ingredients=[dict(x) for x in ingredients]
        existing=next((x for x in ingredients if x['product']==product and x['unit']=='г'),None)
        if existing:existing['amount']=float(existing['amount'])+amount
        else:ingredients.append({'product':product,'amount':amount,'unit':'г'})
        kcal,macros,complete=_recipe_nutrition(ingredients)
        from menu_generator import context, ingredient_price
        _,_,prices,_=context(uid)
        costs=[ingredient_price(x['product'],x['amount'],x['unit'],prices) for x in ingredients]
        price=(f'≈ {sum(costs):.2f} ₽' if all(x is not None for x in costs)
               else 'не все цены известны')
        summary=f'🔥 ≈ {kcal} ккал' if complete else '🔥 Калорийность неполная'
        if macros is not None:
            summary+=f'\nБ {macros[0]:g} · Ж {macros[1]:g} · У {macros[2]:g} г'
        else:summary+='\nБЖУ: неполные сведения'
        text=(f'🥣 {display_title(row["title"])} + {product.lower()}\n{summary}\n'
              f'💰 {price}\n\n'
              + '\n'.join(f"• {x['product']} — {x['amount']:g} {x['unit']}" for x in ingredients)
              + '\n\n⚠️ Предварительный вариант, не сохранён.')
    kb=InlineKeyboardBuilder()
    if choice is None:
        for code,(product,amount) in PORRIDGE_ADDONS.items():
            kb.row(styled(f'+ {product} · {amount} г',f'recipes:extra:{recipe_id}:{code}'))
    kb.row(styled('⬅️⬅️ Добавки',f'recipes:extras:{recipe_id}','primary') if choice is not None else
           styled('⬅️⬅️ Рецепты','recipes:home','primary'),
           styled('❌ Закрыть','recipes:close','danger'))
    return text,kb.as_markup()


def _details(uid, recipe_id, index, offset, *, source=None, group_index=0,virtual_mode=None):
    with connect_db() as db:
        row = db.execute("SELECT * FROM zr_recipes WHERE id=?", (recipe_id,)).fetchone()
        if row is None:raise ValueError("Рецепт не найден")
        favorite = db.execute("SELECT 1 FROM zr_favorites WHERE user_id=? AND recipe_id=?", (uid,recipe_id)).fetchone()
    ingredients = json.loads(row["ingredients_json"])
    from menu_generator import context, ingredient_price
    _, _, prices, _ = context(uid)
    costs=[ingredient_price(i['product'],i['amount'],i['unit'],prices) for i in ingredients]
    price_text=(f"≈ {sum(costs):.2f} ₽ за использованные ингредиенты (не целые упаковки)"
                if all(cost is not None for cost in costs) else "Не все ингредиенты имеют цену")
    kcal,macros,kcal_complete=_recipe_nutrition(ingredients)
    nutrition=(f'🔥 ≈ {kcal} ккал/порция' if kcal_complete else '🔥 Калорийность: недостаточно данных')
    if macros is not None:
        nutrition+=f'\nБ {macros[0]:g} · Ж {macros[1]:g} · У {macros[2]:g} г/порция'
    else:nutrition+='\nБЖУ: неполные справочные данные'
    from recipe_cooking import instructions
    cooking=instructions(row['category'], ingredients, row['preparation'])
    text=(f"🍽 {display_title(row['title'])}\n{nutrition}\n"
          f"{_dish_type(row['category'],row['title'])} · {row['meal']}\n\n"
          "Ингредиенты на порцию, до приготовления:\n"+
          "\n".join(f"• {i['product']} — {i['amount']} {i['unit']}" for i in ingredients)+
          f"\n\n💰 {price_text}\n\nПриготовление: {cooking}\n"
          '⚠️ Предварительный рецепт, КБЖУ и цены ориентировочные.')
    if row['category']=='Каши':
        text+='\n💡 Другие добавки и способы приготовления — в списке вариантов каши.'
    if row['nutrition_status']=='unknown':
        text+='\nНе используется в автоматическом меню: нет проверяемой калорийности.'

    kb = InlineKeyboardBuilder()
    if source=='favorites':
        favorite_callback=f'recipes:ffavorite:{recipe_id}:{offset}'
        back_callback=f'recipes:favs:{offset}'
    elif source=='group':
        favorite_callback=f'recipes:gfavorite:{recipe_id}:{index}:{group_index}:{offset}'
        back_callback=f'recipes:group:{index}:{group_index}:{offset}'
    elif source=='virtual' and virtual_mode in ('pp','gluten_avoid'):
        favorite_callback=f'recipes:vfavorite:{recipe_id}:{virtual_mode}:{offset}'
        back_callback=f'recipes:virtual:{virtual_mode}:{offset}'
    elif source=='vgroup' and virtual_mode in ('regular','pp','gluten_avoid'):
        favorite_callback=f'recipes:vf:{virtual_mode}:{recipe_id}:{index}:{group_index}:{offset}'
        back_callback=f'recipes:vg:{virtual_mode}:{index}:{group_index}:{offset}'
    else:
        favorite_callback=f'recipes:favorite:{recipe_id}:{index}:{offset}'
        back_callback=f'recipes:cat:{index}:{offset}'
    kb.row(styled("❤️ В любимых" if favorite else "🤍 В любимые",
                  favorite_callback, "success" if not favorite else None))
    kb.row(styled("⬅️⬅️ К списку",back_callback,"primary"),
           styled("✖️ Закрыть","recipes:close","danger"))
    return text,kb.as_markup()


async def _edit(call,text,markup):
    from menu_ui import telegram_chunks
    from aiogram.types import InlineKeyboardMarkup
    parts=telegram_chunks(text,3700)
    intermediate=InlineKeyboardMarkup(inline_keyboard=[[styled('❌ Закрыть','recipes:close','danger')]])
    try:
        await call.message.edit_text(parts[0]+('\n\n(продолжение ниже)' if len(parts)>1 else ''),
                                     reply_markup=markup if len(parts)==1 else intermediate)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            await call.message.answer(parts[0],reply_markup=markup if len(parts)==1 else intermediate)
    for number,part in enumerate(parts[1:],1):
        await call.message.answer(f'📖 Продолжение {number+1}/{len(parts)}\n'+part,
                                  reply_markup=markup if number==len(parts)-1 else intermediate)


@router.message(Command("recipes"))
async def home(m:Message,state:FSMContext=None):
    if await check_message(m):
        if state is not None:await state.clear()
        total=sum(row[1] for row in _categories())
        await m.answer(f"📚 Рецепты Жорика · {total} записей\n"
                       "Выберите рубрику. Любимые блюда учитываются при подборе меню, "
                       "если известны их ингредиенты и калорийность.",reply_markup=_category_markup(m.from_user.id))


def _toggle_favorite(uid,recipe_id):
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        found=db.execute('SELECT 1 FROM zr_favorites WHERE user_id=? AND recipe_id=?',(uid,recipe_id)).fetchone()
        if found:
            db.execute('DELETE FROM zr_favorites WHERE user_id=? AND recipe_id=?',(uid,recipe_id))
        else:
            valid=db.execute('SELECT 1 FROM zr_recipes WHERE id=? AND active=1',(recipe_id,)).fetchone()
            if not valid:raise ValueError('Рецепт не найден')
            db.execute('INSERT OR IGNORE INTO zr_favorites VALUES(?,?)',(uid,recipe_id))


@router.callback_query(F.data.startswith("recipes:"))
async def callback(call:CallbackQuery,state:FSMContext=None):
    if not await check_callback(call):return
    parts=(call.data or "").split(":")
    action=parts[1] if len(parts)>1 else ""
    if state is not None:await state.clear()
    await call.answer()
    if action=="close":
        try:await call.message.delete()
        except Exception:await call.message.edit_reply_markup(reply_markup=None)
        return
    if action=="home":
        await _edit(call,"📚 Рецепты Жорика · выберите рубрику",_category_markup(call.from_user.id))
        return
    if action=="back":
        from menu_ui import open_settings_from_inline
        await open_settings_from_inline(call.message,call.from_user.id)
        return
    try:
        if action=='newporridge' and len(parts)==3:
            with connect_db() as db:
                item=db.execute('SELECT title FROM zr_recipes WHERE id=? AND category=?',(parts[2],'Каши')).fetchone()
            if not item:raise ValueError('Не найдена каша')
            title=item['title'].lower().replace('ё','е')
            basis=next((key for token,key in [('овсян','oat'),('греч','buckwheat'),
                              ('рис','rice'),('манн','semolina'),('пшен','millet')]
                        if token in title),None)
            if basis is None:raise ValueError('Для этой каши конструктор пока недоступен')
            porridge_builder.create(call.from_user.id,basis)
            text,markup=porridge_builder.show(call.from_user.id)
        elif action in ('extras','extra') and len(parts) in (3,4):
            text,markup=_porridge_extras(call.from_user.id,parts[2],parts[3] if action=='extra' and len(parts)==4 else None)
        elif action=='virtual' and len(parts)==4:
            # Legacy callbacks may carry an old flat-page offset. The new
            # category screen intentionally has no offset of its own.
            if not parts[3].isdigit():raise ValueError('Неверная страница')
            text,markup=_virtual_page(parts[2])
        elif action=='vc' and len(parts)==4:
            text,markup=_virtual_bases(parts[2],int(parts[3]))
        elif action=='vg' and len(parts)==6:
            text,markup=_virtual_recipes(parts[2],int(parts[3]),int(parts[4]),int(parts[5]))
        elif action in ('vr','vf') and len(parts)==7:
            mode,rid,category_idx,basis_idx,offset=parts[2],parts[3],int(parts[4]),int(parts[5]),int(parts[6])
            if action=='vf':_toggle_favorite(call.from_user.id,rid)
            text,markup=_details(call.from_user.id,rid,category_idx,offset,
                                 source='vgroup',group_index=basis_idx,virtual_mode=mode)
        elif action in ('vdetail','vfavorite') and len(parts)==5:
            rid,virtual_mode,offset=parts[2],parts[3],int(parts[4])
            if action=='vfavorite':_toggle_favorite(call.from_user.id,rid)
            text,markup=_details(call.from_user.id,rid,0,offset,source='virtual',virtual_mode=virtual_mode)
        elif action=='groups' and len(parts)==3:
            text,markup=_groups_page(int(parts[2]))
        elif action=='group' and len(parts)==5:
            text,markup=_group_page(int(parts[2]),int(parts[3]),int(parts[4]))
        elif action=='favs' and len(parts)==3:
            text,markup=_favorites_page(call.from_user.id,int(parts[2]))
        elif action=='fdetail' and len(parts)==4:
            text,markup=_details(call.from_user.id,parts[2],0,int(parts[3]),source='favorites')
        elif action=='ffavorite' and len(parts)==4:
            _toggle_favorite(call.from_user.id,parts[2])
            text,markup=_favorites_page(call.from_user.id,int(parts[3]))
        elif action in ('gdetail','gfavorite') and len(parts)==6:
            recipe_id,index,group_index,offset=parts[2],int(parts[3]),int(parts[4]),int(parts[5])
            if action=='gfavorite':_toggle_favorite(call.from_user.id,recipe_id)
            text,markup=_details(call.from_user.id,recipe_id,index,offset,source='group',group_index=group_index)
        elif action=="cat" and len(parts)==4:
            text,markup=_list_page(int(parts[2]),int(parts[3]))
        elif action in ("detail","favorite") and len(parts)==5:
            recipe_id,index,offset=parts[2],int(parts[3]),int(parts[4])
            if action=="favorite":
                _toggle_favorite(call.from_user.id,recipe_id)
            text,markup=_details(call.from_user.id,recipe_id,index,offset)
        else:return
        await _edit(call,text,markup)
    except (ValueError,IndexError,KeyError) as exc:
        if action in ('extras','extra','newporridge'):
            await call.message.answer('⚠️ '+str(exc)[:180])
        else:
            await call.message.answer('Рецепт или страница недоступны. Откройте «📚 Рецепты» заново.')


def _admin_owner(m):
    return bool(m.from_user and m.from_user.id==settings.sandbox_owner_id and m.chat.type=='private')


@router.message(Command("recipes_export"))
async def owner_export(m:Message):
    if not _admin_owner(m):return
    with connect_db() as db:
        blob=export_json(db)
    await m.answer_document(BufferedInputFile(blob,"zhorik_recipes.json"),
                            caption="📚 Полный справочник. Можно исправить существующие R-рецепты или добавить новые U0001 и т.д.")


@router.message(Command("recipes_import"))
async def owner_import_start(m:Message):
    if not _admin_owner(m):return
    _awaiting_recipe_json.add(m.chat.id)
    await m.answer("📤 Пришли JSON формата zhorik_recipes_v1 до 3 МБ. "
                   "Безопасный режим: только новые U-коды; существующие блюда не заменяются. "
                   "Перед записью будет сделан полный SQLite-бэкап, а после — показан ID партии для точного отката.")


@router.message(Command("recipes_rollback"))
async def owner_recipe_rollback(m:Message):
    if not _admin_owner(m):return
    parts=(m.text or '').split()
    if len(parts)!=2:
        return await m.answer('Использование: /recipes_rollback ID_партии (32 символа)')
    try:
        removed=await asyncio.to_thread(_rollback_with_backup,parts[1])
    except Exception as exc:
        return await m.answer('⚠️ Откат не выполнен: '+str(exc)[:200])
    invalidate_recipe_views()
    await m.answer(f'✅ Удалено ровно {removed} рецептов партии {parts[1]}; остальные не тронуты.')


class AwaitingRecipeJSON(BaseFilter):
    async def __call__(self,m:Message):
        return bool(_admin_owner(m) and m.chat.id in _awaiting_recipe_json and m.document)


@router.message(F.document,AwaitingRecipeJSON())
async def owner_import_file(m:Message):
    _awaiting_recipe_json.discard(m.chat.id)
    if not m.document or not (m.document.file_name or "").lower().endswith(".json") or (m.document.file_size or 0)>3_000_000:
        return await m.answer("Нужен JSON до 3 МБ. Для повтора: /recipes_import")
    try:
        stream=BytesIO()
        await m.bot.download(m.document,destination=stream)
        result=await asyncio.to_thread(_import_with_backup,stream.getvalue())
    except Exception as exc:
        return await m.answer("Импорт не выполнен: "+str(exc)[:200])
    added,batch_id=result
    if added:invalidate_recipe_views()
    await m.answer(f"✅ Новых рецептов: {added}. Существующие не менялись."
                   +(f"\nПартия: {batch_id}\nОткат: /recipes_rollback {batch_id}" if batch_id else "\nПовторный импорт: 0 новых."))


def _import_with_backup(blob):
    # Take a recoverable snapshot before any SQLite mutations.
    from full_db_backup import create_full_backup
    if settings.db_path.exists():create_full_backup()
    with connect_db() as db:
        return import_json(db,blob,append_only=True)


def _rollback_with_backup(batch_id):
    from full_db_backup import create_full_backup
    if settings.db_path.exists():create_full_backup()
    with connect_db() as db:
        return rollback_import_batch(db,batch_id)


@router.callback_query(F.data.startswith('porridge:'))
async def porridge_actions(call:CallbackQuery):
    if not await check_callback(call):return
    parts=(call.data or '').split(':')
    action=parts[1] if len(parts)>1 else ''
    uid=call.from_user.id
    try:
        if action=='home':text,markup=porridge_builder.home()
        elif action=='new' and len(parts)==3:
            porridge_builder.create(uid,parts[2]);text,markup=porridge_builder.show(uid)
        elif action=='liquid' and len(parts)==3:
            porridge_builder.set_liquid(uid,parts[2]);text,markup=porridge_builder.show(uid)
        elif action=='toggle' and len(parts)==3:
            porridge_builder.toggle(uid,parts[2]);text,markup=porridge_builder.show(uid)
        elif action=='quantities':text,markup=porridge_builder.quantities(uid)
        elif action=='qty' and len(parts)==4:
            porridge_builder.change_quantity(uid,parts[2],parts[3]);text,markup=porridge_builder.quantities(uid)
        elif action=='apply':
            text,markup=porridge_builder.show(uid)
            markup=InlineKeyboardBuilder()
            markup.row(styled('❤️ В любимые','porridge:save','success'))
            markup.row(styled('✏️ Изменить добавки','porridge:edit'))
            markup.row(styled('⬅️⬅️ К кашам','porridge:home'),styled('❌ Закрыть','recipes:close','danger'))
            markup=markup.as_markup()
        elif action=='edit':text,markup=porridge_builder.show(uid)
        elif action=='save':
            name=porridge_builder.save(uid)
            text,markup=porridge_builder.favorites(uid)
            text='✅ '+name+' сохранена в любимые.\n\n'+text
        elif action=='favs':text,markup=porridge_builder.favorites(uid)
        elif action=='open' and len(parts)==3:
            porridge_builder.variant(uid,int(parts[2]));text,markup=porridge_builder.show(uid)
        else:return await call.answer('Неверное действие',show_alert=True)
    except (ValueError,KeyError,TypeError) as exc:
        return await call.answer(str(exc)[:190],show_alert=True)
    await call.answer()
    return await _edit(call,text,markup)


@router.callback_query(F.data.startswith('dish:'))
async def dish_actions(call:CallbackQuery):
    if not await check_callback(call):return
    parts=(call.data or '').split(':')
    action=parts[1] if len(parts)>1 else ''
    uid=call.from_user.id
    try:
        if action=='home':text,markup=dish_builder.home()
        elif action=='new' and len(parts)==3:
            dish_builder.start(uid,parts[2]);text,markup=dish_builder.show(uid)
        elif action in ('base','method','side') and len(parts)==3:
            dish_builder.choose(uid,action,parts[2]);text,markup=dish_builder.show(uid)
        elif action=='toggle' and len(parts)==3:
            dish_builder.toggle(uid,parts[2]);text,markup=dish_builder.show(uid)
        elif action=='save':
            title=dish_builder.save(uid)
            text,markup=dish_builder.favorites(uid)
            text='✅ '+title+' сохранено.\n\n'+text
        elif action=='favs':text,markup=dish_builder.favorites(uid)
        elif action=='open' and len(parts)==3:
            dish_builder.load(uid,int(parts[2]));text,markup=dish_builder.show(uid)
        else:return await call.answer('Неверное действие',show_alert=True)
    except (ValueError,KeyError,TypeError) as exc:
        return await call.answer(str(exc)[:190],show_alert=True)
    await call.answer()
    return await _edit(call,text,markup)
