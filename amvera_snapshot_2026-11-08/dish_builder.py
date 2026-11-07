"""REV5 general recipe construction engine.

The same immutable ingredient snapshot, allergy screening and calorie/price
calculation power proteins+side dishes, salads and curd breakfasts. No catalog
recipes are modified; no external product values are invented.
"""
from __future__ import annotations
import json
from database import connect_db
from keyboards import styled
from aiogram.types import InlineKeyboardMarkup

PROTEINS={
 'chicken':('Куриное филе',190), 'beef':('Говядина',180),
 'pork':('Свинина',180), 'turkey':('Индейка филе',190),
 'fish':('Минтай',200), 'sausage':('Сосиски',150),
 'cutlet':('Котлеты',150),
}
SIDES={
 'none':('Без гарнира',()), 'rice':('Рис',(('Рис',75),)),
 'buckwheat':('Гречка',(('Гречка',75),)),
 'potato':('Картофель',(('Картофель',200),)),
 'pasta':('Макароны',(('Макароны',85),)),
 'vegetables':('Тушёные овощи',(('Капуста белокочанная',120),('Морковь',70))),
}
METHODS={'stew':'Тушёное','bake':'Запечённое','boil':'Варёное','fry':'Жареное'}
VEGETABLES={
 'cucumber':('Огурцы',70), 'tomato':('Помидоры',70),
 'pepper':('Перец сладкий',50), 'cabbage':('Капуста белокочанная',60),
 'beet':('Свёкла',70), 'carrot':('Морковь',50),
}
FRUITS={
 'apple':('Яблоки',60),'banana':('Бананы',60),
 'pear':('Груши',60), 'berries':('Ягоды',50),
}
DRESSINGS={'none':('Без заправки',()), 'oil':('Масло растительное',(('Масло подсолнечное',10),)),
           'sour':('Сметана',(('Сметана',30),))}
CURD_ADDONS={'none':('Без добавок',()),'apple':('Яблоко',(('Яблоки',50),)),
             'banana':('Банан',(('Бананы',50),)),
             'honey':('Мёд',(('Мёд',10),)),
             'sour':('Сметана',(('Сметана',30),)),
             'jam':('Варенье',(('Варенье',20),))}
SOUPS={
 'veg':('Овощной суп',(('Картофель',120),('Морковь',50),('Капуста белокочанная',100))),
 'chicken':('Куриный суп',(('Куриное филе',100),('Картофель',120),('Морковь',50))),
 'shchi':('Щи',(('Капуста белокочанная',180),('Картофель',90),('Морковь',50))),
 'fish':('Рыбный суп',(('Минтай',140),('Картофель',100),('Морковь',50))),
}
CATEGORIES={'main':'🍗 Основное + гарнир','vegetable':'🥗 Овощной салат',
            'fruit':'🍎 Фруктовый салат','curd':'🧀 Творог + добавки','soup':'🍲 Супы'}


def init():
    with connect_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS zm_recipe_variants (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, recipe_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
        db.execute('''CREATE TABLE IF NOT EXISTS zm_dish_drafts (
          owner_id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
          base TEXT NOT NULL DEFAULT '',method TEXT NOT NULL DEFAULT 'stew',
          side TEXT NOT NULL DEFAULT 'none', additions_json TEXT NOT NULL DEFAULT '{}')''')


def start(uid,kind):
    if kind not in CATEGORIES:raise ValueError('Тип блюда неизвестен')
    default_base='chicken' if kind=='main' else ('veg' if kind=='soup' else 'none')
    with connect_db() as db:
        db.execute('''INSERT INTO zm_dish_drafts(owner_id,kind,base,method,side,additions_json)
        VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET kind=excluded.kind,
        base=excluded.base,method=excluded.method,side=excluded.side,
        additions_json=excluded.additions_json''',
        (uid,kind,default_base,'stew','none','{}'))


def get(uid):
    with connect_db() as db:
        row=db.execute('SELECT * FROM zm_dish_drafts WHERE owner_id=?',(uid,)).fetchone()
    if not row:raise ValueError('Откройте конструктор сначала')
    return dict(row)


def choose(uid,field,value):
    state=get(uid);kind=state['kind']
    allowed={'base':PROTEINS if kind=='main' else SOUPS if kind=='soup' else {},
             'side':SIDES if kind=='main' else DRESSINGS if kind in ('vegetable','fruit') else CURD_ADDONS if kind=='curd' else {},
             'method':METHODS if kind=='main' else {}}
    if field not in allowed or value not in allowed[field]:raise ValueError('Неверный вариант')
    with connect_db() as db:
        db.execute('UPDATE zm_dish_drafts SET '+field+'=? WHERE owner_id=?',(value,uid))


def toggle(uid,code):
    state=get(uid);kind=state['kind']
    choices=VEGETABLES if kind=='vegetable' else FRUITS if kind=='fruit' else None
    if choices is None or code not in choices:raise ValueError('Недоступная добавка')
    selected=json.loads(state['additions_json'])
    if code in selected:del selected[code]
    else:
        from menu_generator import context,blocked
        from menu_diet import get as get_diet,recipe_is_eligible
        _,prefs,_,_=context(uid)
        product,grams=choices[code]
        if blocked(product,prefs) or not recipe_is_eligible([(product,grams)],get_diet(uid)):
            raise ValueError('Ингредиент исключён настройками или аллергенами')
        selected[code]=grams
    with connect_db() as db:
        db.execute('UPDATE zm_dish_drafts SET additions_json=? WHERE owner_id=?',
                   (json.dumps(selected,ensure_ascii=False),uid))


def assembled(uid):
    state=get(uid);kind=state['kind'];part=[]
    if kind=='main':
        base=PROTEINS[state['base']]
        method=METHODS[state['method']]
        side_name,side_ingredients=SIDES[state['side']]
        name=base[0]+' · '+method.lower()+(' + '+side_name.lower() if state['side']!='none' else '')
        part=[(base[0],base[1]),*side_ingredients]
        if state['method']=='fry':part.append(('Масло подсолнечное',8))
    elif kind=='soup':
        name,base_ingredients=SOUPS[state['base']]
        part=list(base_ingredients)
    elif kind in ('fruit','vegetable'):
        chosen=json.loads(state['additions_json'])
        choices=FRUITS if kind=='fruit' else VEGETABLES
        if not chosen:raise ValueError('Выберите ингредиенты салата')
        name='Фруктовый салат' if kind=='fruit' else 'Овощной салат'
        part=[(choices[k][0],v) for k,v in chosen.items() if k in choices]
        _,dressing=DRESSINGS[state['side']]
        part+=list(dressing)
    elif kind=='curd':
        name='Творог'
        part=[('Творог',150)]
        label,addition=CURD_ADDONS[state['side']]
        if addition:name+=' + '+label.lower();part+=list(addition)
    else:raise ValueError('Нет такого блюда')
    return name,[{'product':p,'unit':'г','amount':g} for p,g in part]


def checked(uid):
    from menu_generator import context,blocked,ingredient_price
    from menu_diet import get as current_diet,recipe_is_eligible
    from recipe_ui import _recipe_nutrition
    name,ingredients=assembled(uid)
    _,prefs,prices,_=context(uid)
    pairs=[(x['product'],x['amount']) for x in ingredients]
    if any(blocked(p,prefs) for p,_ in pairs):raise ValueError('В рецепте есть исключённый продукт или аллерген')
    if not recipe_is_eligible(pairs,current_diet(uid),title=name):
        raise ValueError('Рецепт не подходит выбранному питанию')
    kcal,macros,full=_recipe_nutrition(ingredients)
    costs=[ingredient_price(x['product'],x['amount'],x['unit'],prices) for x in ingredients]
    price=f'{sum(costs):.2f} ₽' if all(x is not None for x in costs) else 'не все цены известны'
    nut=(f'🔥 ≈ {kcal} ккал' if full else '🔥 Калорийность неполная')
    nut+=f' · Б {macros[0]:g} / Ж {macros[1]:g} / У {macros[2]:g} г' if macros else '\nБЖУ: неполные сведения'
    text=(f'🍽 {name}\n{nut}\n💰 {price}\n\n'+
          '\n'.join(f"• {i['product']} — {i['amount']:g} г" for i in ingredients))
    text+='\n\n⚠️ Предварительный вариант. Вес ингредиентов до приготовления.'
    return text,name,ingredients


def home():
    rows=[[styled(title,'dish:new:'+key)] for key,title in CATEGORIES.items()]
    rows.extend([[styled('❤️ Мои блюда','dish:favs')], [styled('⬅️⬅️ Рецепты','recipes:home')]])
    return '🍽 Конструктор блюд · выберите тип',InlineKeyboardMarkup(inline_keyboard=rows)


def show(uid):
    state=get(uid);kind=state['kind']
    try:text,_,_=checked(uid)
    except ValueError as exc:text='🍽 '+CATEGORIES[kind]+'\n'+str(exc)
    buttons=[]
    if kind in ('main','soup'):
        opts=PROTEINS if kind=='main' else SOUPS
        buttons.extend([[styled(('✅ ' if state['base']==k else '⬜ ')+v[0],f'dish:base:{k}')]
                        for k,v in opts.items()])
    if kind=='main':
        buttons.extend([[styled(('✅ ' if state['method']==k else '⬜ ')+v,f'dish:method:{k}')]
                        for k,v in METHODS.items()])
    if kind in ('vegetable','fruit'):
        opts=VEGETABLES if kind=='vegetable' else FRUITS
        selected=json.loads(state['additions_json'])
        for k,(p,g) in opts.items():
            buttons.append([styled(('✅ ' if k in selected else '⬜ ')+f'{p} · {g} г',f'dish:toggle:{k}')])
    if kind!='soup':
        choices=SIDES if kind=='main' else CURD_ADDONS if kind=='curd' else DRESSINGS
        for k,(label,_) in choices.items():
            buttons.append([styled(('✅ ' if state['side']==k else '⬜ ')+label,f'dish:side:{k}')])
    buttons.append([styled('❤️ Сохранить вариант','dish:save','success')])
    buttons.append([styled('⬅️⬅️ Тип блюда','dish:home'),styled('❌ Закрыть','recipes:close','danger')])
    return text,InlineKeyboardMarkup(inline_keyboard=buttons)


def save(uid):
    text,title,ingredients=checked(uid)
    state=get(uid)
    data={'kind':state['kind'],'base':state['base'],'method':state['method'],
          'side':state['side'],'additions':json.loads(state['additions_json']),
          'ingredients':ingredients}
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute('SELECT recipe_json FROM zm_recipe_variants WHERE owner_id=? AND kind=?',(uid,state['kind'])):
            try:
                if _signature(json.loads(row['recipe_json']))==_signature(data):return title  # identical dish already saved
            except (ValueError,TypeError,AttributeError):continue
        db.execute('INSERT INTO zm_recipe_variants(owner_id,kind,title,recipe_json) VALUES(?,?,?,?)',
                   (uid,state['kind'],title,json.dumps(data,ensure_ascii=False)))
    return title


def _signature(data):
    return json.dumps([data.get('kind'),data.get('base'),data.get('method'),data.get('side'),
                       sorted((data.get('additions') or {}).items())],ensure_ascii=False)


def favorites(uid):
    with connect_db() as db:
        rows=db.execute("SELECT id,title FROM zm_recipe_variants WHERE owner_id=? AND kind!='porridge' ORDER BY id DESC LIMIT 25",(uid,)).fetchall()
    buttons=[[styled(r['title'][:38],f"dish:open:{r['id']}")] for r in rows]
    buttons.append([styled('⬅️⬅️ Конструктор','dish:home')])
    return ('❤️ Мои блюда' if rows else 'Сохранённых блюд пока нет.'),InlineKeyboardMarkup(inline_keyboard=buttons)


def load(uid,variant_id):
    with connect_db() as db:
        row=db.execute('SELECT recipe_json,kind FROM zm_recipe_variants WHERE owner_id=? AND id=?',(uid,variant_id)).fetchone()
        if not row or row['kind']=='porridge':raise ValueError('Блюдо не найдено')
        data=json.loads(row['recipe_json'])
        db.execute('''INSERT INTO zm_dish_drafts(owner_id,kind,base,method,side,additions_json)
          VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET
          kind=excluded.kind,base=excluded.base,method=excluded.method,
          side=excluded.side,additions_json=excluded.additions_json''',
          (uid,data['kind'],data['base'],data['method'],data['side'],json.dumps(data['additions'])))
