"""REV5 porridge reference builder: one base + liquid + checked additions.

User variants are separate records. Existing R-codes and favorites are untouched.
"""
import json
from database import connect_db
from keyboards import styled
from aiogram.types import InlineKeyboardMarkup

BASES={
 'oat':('Овсяная каша','Овсяные хлопья',60),
 'rice':('Рисовая каша','Рис',60),
 'buckwheat':('Гречневая каша','Гречка',60),
 'semolina':('Манная каша','Манная крупа',50),
 'millet':('Пшённая каша','Пшено',60),
}
ADDONS={
 'apple':('Яблоки',50),'banana':('Бананы',50),'pear':('Груши',50),
 'berries':('Ягоды',50),'prunes':('Чернослив',25),
 'apricots':('Курага',25),'raisins':('Изюм',20),
 'nuts':('Орехи',15),'honey':('Мёд',10),'curd':('Творог',50),
 'butter':('Масло сливочное',5),
}


def init():
    with connect_db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS zm_porridge_drafts (
            owner_id INTEGER PRIMARY KEY, base TEXT NOT NULL,
            liquid TEXT NOT NULL, additions_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS zm_recipe_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
            kind TEXT NOT NULL, title TEXT NOT NULL,
            recipe_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS zm_variants_owner ON zm_recipe_variants(owner_id,id);
        ''')


def create(uid,base):
    if base not in BASES:raise ValueError('Неизвестная каша')
    with connect_db() as db:
        db.execute('''INSERT INTO zm_porridge_drafts(owner_id,base,liquid,additions_json)
            VALUES(?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET
            base=excluded.base,liquid=excluded.liquid,additions_json=excluded.additions_json''',
            (uid,base,'water','{}'))


def _ensure_allowed(uid,parts):
    """Validate ingredients BEFORE anything is persisted."""
    from menu_generator import context,blocked
    from menu_diet import get,recipe_is_eligible
    _,prefs,_,_=context(uid)
    for item in parts:
        if blocked(item['product'],prefs):raise ValueError('Исключённый продукт или аллерген: '+item['product'])
    # Favourite variants are personal drafts; diet eligibility is checked on weekly menu insertion.


def set_liquid(uid,liquid):
    if liquid not in ('water','milk'):raise ValueError('Выберите воду или молоко')
    if liquid=='milk':
        try:_ensure_allowed(uid,[{'product':'Молоко','amount':200}])
        except ValueError:raise ValueError('Молоко исключено вашими настройками или типом питания') from None
    with connect_db() as db:
        db.execute('UPDATE zm_porridge_drafts SET liquid=? WHERE owner_id=?',(liquid,uid))


def draft(uid):
    with connect_db() as db:
        row=db.execute('SELECT base,liquid,additions_json FROM zm_porridge_drafts WHERE owner_id=?',(uid,)).fetchone()
    if not row:raise ValueError('Сначала выберите кашу')
    return row['base'],row['liquid'],json.loads(row['additions_json'])


def toggle(uid,addon):
    if addon not in ADDONS:raise ValueError('Нет такой добавки')
    base,liquid,addons=draft(uid)
    if addon not in addons:
        from menu_generator import context,blocked
        from menu_diet import get,recipe_is_eligible
        _,prefs,_,_=context(uid)
        product,grams=ADDONS[addon]
        if blocked(product,prefs):raise ValueError('Добавка запрещена пищевыми исключениями или аллергенами')
        # Dietary suitability does not prohibit building or saving a personal favourite.
        addons[addon]=grams
    else:del addons[addon]
    with connect_db() as db:
        db.execute('UPDATE zm_porridge_drafts SET additions_json=? WHERE owner_id=?',
                   (json.dumps(addons,ensure_ascii=False),uid))


def change_quantity(uid,code,direction):
    if code not in ADDONS or direction not in ('up','down'):
        raise ValueError('Неверная добавка или действие')
    _,_,addons=draft(uid)
    if code not in addons:raise ValueError('Сначала отметьте добавку галочкой')
    addon_step=5 if ADDONS[code][1] <= 25 else 10
    current=int(addons[code])
    caps={'butter':30,'honey':30,'nuts':50,'raisins':60,'prunes':80,'apricots':80}
    addons[code]=max(5,min(caps.get(code,200),current+(addon_step if direction=='up' else -addon_step)))
    with connect_db() as db:
        db.execute('UPDATE zm_porridge_drafts SET additions_json=? WHERE owner_id=?',
                   (json.dumps(addons,ensure_ascii=False),uid))


def quantities(uid):
    _,_,addons=draft(uid)
    rows=[]
    for code,amount in addons.items():
        if code not in ADDONS:continue
        product=ADDONS[code][0]
        rows.append([styled('➖',f'porridge:qty:{code}:down'),
                     styled(f'{product} · {amount} г','porridge:quantities'),
                     styled('➕',f'porridge:qty:{code}:up')])
    rows.append([styled('⬅️⬅️ К добавкам','porridge:edit')])
    return ('✏️ Количество добавок на порцию' if addons else 'Сначала выберите добавки.'),InlineKeyboardMarkup(inline_keyboard=rows)


def ingredients(uid):
    base,liquid,addons=draft(uid)
    title,product,grams=BASES[base]
    parts=[{'product':product,'unit':'г','amount':grams}]
    if liquid=='milk':parts.append({'product':'Молоко','unit':'г','amount':200})
    for code,amount in addons.items():
        if code in ADDONS:parts.append({'product':ADDONS[code][0],'unit':'г','amount':amount})
    return title+(' на молоке' if liquid=='milk' else ' на воде'),parts


def _instruction(base,liquid):
    if base=='oat':
        note=('Вскипятите около 200 мл молока, добавьте хлопья и варите, помешивая.' if liquid=='milk'
              else 'Вскипятите около 200 мл воды, добавьте хлопья и варите, помешивая.')
        return note+' Время зависит от вида хлопьев: следуйте инструкции производителя. Здесь приведён общий способ.'
    if base=='semolina':
        return 'Вскипятите около 250 мл '+('молока' if liquid=='milk' else 'воды')+', всыпайте манку тонкой струйкой, постоянно помешивая. Варите до загустения, ориентируясь на упаковку.'
    if base=='buckwheat':
        return 'Промойте гречку. Добавьте примерно 120–150 мл '+('воды, затем молоко по рецепту' if liquid=='milk' else 'воды')+'. Варите до готовности, сверяясь с упаковкой.'
    if base=='rice':
        return 'Промойте рис. Варите в воде до полуготовности, затем '+('добавьте молоко и доведите до мягкости.' if liquid=='milk' else 'доведите до мягкости, при необходимости добавляя воду.')
    return 'Промойте пшено до прозрачной воды и обдайте горячей водой. Варите в воде до мягкости'+(', затем добавьте молоко.' if liquid=='milk' else '.')


def _info(uid):
    from recipe_ui import _recipe_nutrition
    from menu_generator import context,ingredient_price,blocked
    from menu_diet import get,recipe_is_eligible
    base,liquid,addons=draft(uid)
    name,parts=ingredients(uid)
    _,prefs,prices,_=context(uid)
    for item in parts:
        if blocked(item['product'],prefs):raise ValueError('Исключённый продукт или аллерген: '+item['product'])
    # Favourite variants are personal drafts; diet eligibility is checked on weekly menu insertion.
    kcal,macros,complete=_recipe_nutrition(parts)
    costs=[ingredient_price(x['product'],x['amount'],x['unit'],prices) for x in parts]
    price=f'{sum(costs):.2f} ₽' if all(v is not None for v in costs) else 'не все цены известны'
    nutrition=(f'🔥 ≈ {kcal} ккал' if complete else '🔥 Калорийность неполная')
    nutrition+= (f' · Б {macros[0]:g} / Ж {macros[1]:g} / У {macros[2]:g} г' if macros else '\nБЖУ: неполные сведения')
    text=(f'🥣 {name}\n{nutrition}\n💰 {price}\n\n'+
          '\n'.join(f"• {x['product']} — {x['amount']:g} {x['unit']}" for x in parts))
    text+='\n\nПриготовление: '+_instruction(base,liquid)
    if addons:text+='\nДобавки добавляйте к готовой каше, учитывая их свойства.'
    text+='\n⚠️ Расчёт предварительный. Вода для варки не включена в стоимость.'
    return text,name,parts


def show(uid):
    base,liquid,addons=draft(uid)
    try:text,_,_=_info(uid)
    except ValueError as exc:
        # Allow removal of a now-forbidden ingredient without trapping the user.
        text='⚠️ '+str(exc)+'\nПроверьте отмеченные ингредиенты и снимите именно запрещённый продукт.'
    rows=[[styled(('✅ ' if liquid==v else '⬜ ')+label,f'porridge:liquid:{v}') for v,label in
           [('water','💧 На воде'),('milk','🥛 На молоке')]]]
    options=list(ADDONS.items())
    for i in range(0,len(options),2):
        rows.append([styled(('✅ ' if code in addons else '⬜ ')+product+' '+str(grams)+' г',
                            'porridge:toggle:'+code) for code,(product,grams) in options[i:i+2]])
    if addons:rows.append([styled('⚖️ Изменить граммовки','porridge:quantities')])
    rows.extend([[styled('✅ Применить','porridge:apply','success')],
                 [styled('⬅️⬅️ Каши','porridge:home'),styled('❌ Закрыть','recipes:close','danger')]])
    return text,InlineKeyboardMarkup(inline_keyboard=rows)


def home():
    rows=[[styled(title,'porridge:new:'+key)] for key,(title,_,_) in BASES.items()]
    rows.append([styled('❤️ Мои варианты каш','porridge:favs')])
    rows.append([styled('⬅️⬅️ Рецепты','recipes:home')])
    return '🥣 Каши: выберите крупу. Фрукты и другие добавки выбираются далее.',InlineKeyboardMarkup(inline_keyboard=rows)


def save(uid):
    text,name,parts=_info(uid)
    base,liquid,addons=draft(uid)
    data={'base':base,'liquid':liquid,'addons':addons,'ingredients':parts,'preparation':_instruction(base,liquid)}
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT recipe_json FROM zm_recipe_variants WHERE owner_id=? AND kind='porridge'",(uid,)):
            try:
                if _signature(json.loads(row['recipe_json']))==_signature(data):return name  # identical variant already saved
            except (ValueError,TypeError,AttributeError):continue
        db.execute('''INSERT INTO zm_recipe_variants(owner_id,kind,title,recipe_json)
            VALUES(?,?,?,?)''',(uid,'porridge',name,json.dumps(data,ensure_ascii=False)))
    return name


def _signature(data):
    return json.dumps([data.get('base'),data.get('liquid'),sorted((data.get('addons') or {}).items())],ensure_ascii=False)


def favorites(uid):
    with connect_db() as db:
        rows=db.execute('SELECT id,title,recipe_json FROM zm_recipe_variants WHERE owner_id=? ORDER BY id DESC LIMIT 25',(uid,)).fetchall()
    buttons=[[styled(f"❤️ {r['title']}",f"porridge:open:{r['id']}")]
             for r in rows]
    buttons.append([styled('⬅️⬅️ Каши','porridge:home')])
    return ('❤️ Любимые варианты каш' if rows else 'Любимых вариантов каш пока нет.'),InlineKeyboardMarkup(inline_keyboard=buttons)


def variant(uid,variant_id):
    with connect_db() as db:
        row=db.execute('SELECT recipe_json,title FROM zm_recipe_variants WHERE owner_id=? AND id=?',(uid,variant_id)).fetchone()
    if not row:raise ValueError('Сохранённый вариант не найден')
    data=json.loads(row['recipe_json'])
    if data.get('base') not in BASES or data.get('liquid') not in ('water','milk'):
        raise ValueError('Сохранённый вариант устарел')
    _ensure_allowed(uid,data['ingredients'])  # allergies may have changed since it was saved
    with connect_db() as db:
        db.execute('''INSERT INTO zm_porridge_drafts(owner_id,base,liquid,additions_json)
         VALUES(?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET
         base=excluded.base,liquid=excluded.liquid,additions_json=excluded.additions_json''',
                   (uid,data['base'],data['liquid'],json.dumps(data['addons'],ensure_ascii=False)))
