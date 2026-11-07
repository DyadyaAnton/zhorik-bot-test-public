"""Per-user menu modes, separated from preference and allergy persistence.

Gluten screening is deliberately conservative. It never certifies a recipe,
brand, ingredient or kitchen as free of gluten or cross-contact.
"""
from database import connect_db

MODES={'regular':'🍽 Обычное','pp':'🥗 ПП','gluten_avoid':'🌾 Без глютена'}
GLUTEN_UNVERIFIED={'макароны','хлеб ржаной','хлеб белый','мука',
                   'овсяные хлопья','манная крупа','перловая крупа',
                   'крупа ячневая','пшеничная крупа','булгур','кус-кус',
                   'пшеница','рожь','ячмень','овес','овёс'}
GLUTEN_RISK_WORDS=('хлеб','мука','макарон','лапша','вермишель','спагетти',
                   'булгур','кускус','полба','манн','перлов','ячнев',
                   'пшен','ржан','овсян','овес','геркулес','мюсли',
                   'гранола','лаваш','тортиль','сухари','батон',
                   'печень','вафл','бисквит','пирог','соевый соус',
                   'колбас','сосиск','ветчин','паниров',
                   'сухая смесь','соус готовый')
PP_DESSERT_WORDS=('торт','бисквит','пирожн','пирог','печенье',
                  'сладкая запеканка','кекс','конфет','вафл')


def init():
    with connect_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS zm_menu_modes (
             user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'regular',
             updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')


def get(uid):
    with connect_db() as db:
        row=db.execute('SELECT mode FROM zm_menu_modes WHERE user_id=?',(uid,)).fetchone()
    value=row['mode'] if row else 'regular'
    return value if value in MODES else 'regular'


def set_mode(uid,mode):
    if mode not in MODES:raise ValueError('Неизвестный тип питания')
    with connect_db() as db:
        db.execute('''INSERT INTO zm_menu_modes(user_id,mode) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode,updated_at=CURRENT_TIMESTAMP
            WHERE zm_menu_modes.mode!=excluded.mode''',(uid,mode))


def gluten_screen(name):
    """False if gluten/gluten-cross-contact risk cannot be screened out."""
    value=name.casefold().replace('ё','е').strip()
    return value not in GLUTEN_UNVERIFIED and not any(word in value for word in GLUTEN_RISK_WORDS)


def recipe_is_eligible(parts,mode,title='',category=''):
    if mode=='gluten_avoid':
        return all(gluten_screen(name) for name,_ in parts)
    if mode=='pp':
        # Editorial screening only, not a clinical definition of nutrition.
        label=(title+' '+category).casefold().replace('ё','е')
        if any(token in label for token in PP_DESSERT_WORDS) or 'торты и десерты' in label:
            return False
        return all(name.casefold().replace('ё','е') not in {'сахар','шоколад'} for name,_ in parts)
    return True


def validate_menu(menu,mode):
    if mode=='regular':return
    for day in menu.get('menu',{}).values():
        for dishes in day.values():
            for dish in dishes:
                parts=[(i['product'],i['amount']) for i in dish['ingredients']]
                if not recipe_is_eligible(parts,mode,title=dish['name']):
                    from menu_generator import MenuInvalid
                    raise MenuInvalid('Блюдо не прошло проверку выбранного типа питания: '+dish['name'])


DISCLAIMER=('🌾 Проверены только названия ингредиентов. Безглютеновый состав, '
            'маркировка упаковок и отсутствие следов глютена НЕ подтверждены. '
            'При целиакии или аллергии проверяйте конкретные продукты.')
