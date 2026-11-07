"""Validation and deterministic estimates. Unknown data is never invented."""
import asyncio
import json
import re
from datetime import date,timedelta
from database import connect_db
from calorie_catalog import energy
from price07_menu_products import reviewed_menu_products, menu_energy, menu_blocked
from llm_service import generate_json,LLMError
from planning_prices import prices_for_menu, purchase_estimate, assumption, _key
from price_b_catalog import seeds
import menu_rules
import unit_conversions

DAYS=('Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье')
MEALS=('Завтрак','Обед','Ужин')
ALLERGEN_GROUPS={
 'молоко':{'молоко','сливки','сметана','кефир','творог','сыр твердый','сыр плавленый','масло сливочное','йогурт','ряженка','моцарелла','брынза'},
 'яйца':{'яйца','яйцо','яйцо куриное','яйца куриные','майонез'},
 'арахис':{'арахис','арахис жареный','арахисовая паста'},
 'орехи':{'орехи','миндаль','фундук','грецкие орехи','орех грецкий',
          'кешью','фисташки','пекан','макадамия','кедровые орехи',
          'бразильский орех'},
 'рыба':{'минтай','хек','треска','скумбрия','горбуша','лосось','тунец консервированный','сардины','сельдь'},
 'пшеница':{'мука','макароны','спагетти','хлеб белый','батон','булочки','манка'},
 'глютен':{'мука','макароны','спагетти','хлеб белый','хлеб ржаной','батон','булочки','манка','овсяные хлопья','перловка','ячневая крупа'},
}

# Catalog-based identity is authoritative for known CP products. A lexical
# marker alone misses e.g. 'Простокваша' and 'Фета'. Do not classify the whole
# fish/seafood category as fish: shrimp, mussels and vegetable caviar are not
# fish and must not become false positives.
_NON_FISH_IN_SEAFOOD={'кальмар','креветки','мидии','икра кабачковая'}
_CATALOG_WHEAT={
    'манная крупа','булгур','кускус','полба','макароны из твердых сортов',
    'макароны обычные','лапша яичная','вермишель','спагетти',
    'хлеб пшеничный','хлеб цельнозерновой','батон нарезной','лаваш',
    'тортилья','сухари панировочные','мука пшеничная высший сорт',
    'мука пшеничная цельнозерновая',
}
_CATALOG_GLUTEN=_CATALOG_WHEAT | {
    'перловая крупа','ячневая крупа','хлеб ржаной','хлебцы ржаные',
    'мука ржаная',
    # These generic names do not certify gluten-free manufacture; in a strict
    # profile they are unavailable until a particular composition is known.
    'овсяные хлопья','овсяная крупа','геркулес','мюсли без сахара','гранола',
}
_CATALOG_EGGS={'лапша яичная'}
_CATALOG_NUTS={'грецкие орехи','миндаль'}
_CATALOG_PEANUT={'арахис','арахисовая паста'}
_KNOWN_ALLERGEN_PRODUCTS={
    'молоко':frozenset(name.casefold().replace('ё','е')
                      for _,name,category,_ in seeds()
                      if category=='Молоко и молочные'),
    'рыба':frozenset(name.casefold().replace('ё','е')
                    for _,name,category,_ in seeds()
                    if category=='Рыба и морепродукты'
                    and name.casefold().replace('ё','е') not in _NON_FISH_IN_SEAFOOD),
    'пшеница':frozenset(_CATALOG_WHEAT),
    'глютен':frozenset(_CATALOG_GLUTEN),
    'яйца':frozenset(_CATALOG_EGGS | {
        name.casefold().replace('ё','е') for _,name,category,_ in seeds()
        if category=='Яйца и бобовые' and name.startswith('Яйца ')}),
    'орехи':frozenset(_CATALOG_NUTS),
    'арахис':frozenset(_CATALOG_PEANUT),
}
class MenuInvalid(ValueError):pass


class BudgetExceeded(MenuInvalid):
    """Меню нельзя уложить в бюджет с допуском (см. menu_rules). Несёт данные для трёх путей выхода."""
    def __init__(self,message,budget,needed):
        super().__init__(message)
        def _num(value):
            try:return float(value)
            except (TypeError,ValueError):return float('nan')
        self.budget=_num(budget)
        self.needed=_num(needed)


class PriceMap(dict):
    """Regular price mapping with transparent quality metadata."""
    def __init__(self, *args, estimate_keys=(), provenance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.estimate_keys = set(estimate_keys)
        self.provenance = dict(provenance or {})

def context(uid):
    with connect_db() as db:
        products=[dict(r) for r in db.execute('SELECT name,unit,category FROM products ORDER BY name')]
        discovered=reviewed_menu_products(db, (p['name'] for p in products))
        prefs={k:[r['value'].casefold() for r in db.execute('SELECT value FROM preferences WHERE telegram_id=? AND preference_type=? AND active=1',(uid,k))] for k in ('allergy','dislike','like')}
        prices=[dict(r) for r in db.execute('SELECT product,unit,price,observed_at,shop FROM user_prices WHERE user_id=?',(uid,))]
        setting=db.execute('SELECT calorie_target FROM user_settings WHERE telegram_id=?',(uid,)).fetchone()
        city_row=db.execute('SELECT city FROM user_cities WHERE user_id=?',(uid,)).fetchone()
        city=city_row['city'] if city_row else None
        selected_shops=[row['shop'] for row in db.execute('SELECT shop FROM user_shops WHERE user_id=?',(uid,))]
        # No inferred city or shop. Verified retailer observations become
        # available only when their scoped provenance matches this user.
        planning, provisional, source_map = prices_for_menu(db,city=city,selected_shops=selected_shops,
                                                           include_provenance=True)
    # Preserve the existing catalog identities; expose all 258 additional
    # canonical ingredients without deleting the old 30-ingredient vocabulary.
    existing={_key(p['name']) for p in products}
    for _,name,_,unit in seeds():
        if _key(name) not in existing:
            products.append({'name':name,'unit':unit})
            existing.add(_key(name))
    # A discovery may overlap the canonical seeded catalog. Never shadow its
    # established food identity, and never infer packaging or a store price.
    products.extend(p for p in discovered if _key(p['name']) not in existing)
    # Multiple store observations are averaged only for matching product and unit.
    grouped={}
    for p in prices:
        if p['observed_at']>=(date.today()-timedelta(days=3)).isoformat():
            grouped.setdefault((p['product'].casefold(),p['unit']),[]).append(p)
    prices=PriceMap(planning,estimate_keys=provisional,provenance=source_map)
    # Legacy recipe ingredients are valid even when they have no strict
    # one-to-one CP equivalent. Use a separately labeled planning assumption.
    category_map={'Молочные':'Молоко и молочные', 'Рыба':'Рыба и морепродукты',
                  'Фрукты':'Фрукты и ягоды', 'Бакалея':'Напитки и прочее',
                  'Напитки':'Напитки и прочее', 'Прочее':'Напитки и прочее',
                  'Хлеб и выпечка':'Хлеб и мука'}
    for p in products:
        if 'price07_code' in p:
            continue  # No synthetic estimated prices for an audited new product.
        name_unit=(_key(p['name']),p['unit'])
        if name_unit not in prices:
            category=category_map.get(p.get('category'),p.get('category'))
            if category not in ('Молоко и молочные','Мясо и птица','Рыба и морепродукты',
                                'Крупы и макароны','Овощи','Фрукты и ягоды',
                                'Яйца и бобовые','Хлеб и мука','Масла и приправы','Напитки и прочее'):
                category='Напитки и прочее'
            prices[name_unit]=float(assumption(p['name'],category))
            prices.estimate_keys.add(name_unit)
            prices.provenance[name_unit]={'quality':'planning_assumption',
                                          'source':'Условная оценка; не магазинная цена'}
    for key, offers in grouped.items():
        norm_key=(_key(key[0]),key[1])
        prices[norm_key]=sum(p['price'] for p in offers)/len(offers)
        prices.estimate_keys.discard(norm_key)
        prices.provenance[norm_key]={'quality':'personal','source':'Личное предложение пользователя',
                                     'observed_at':max(p['observed_at'] for p in offers),
                                     'shops':sorted({str(p['shop']).strip() for p in offers if p['shop']})}
    return products,prefs,prices,setting['calorie_target'] if setting else None

# Explicitly supported individual allergens; do not treat arbitrary unknown words
# as safe, and never use substring matching (e.g. "лук" in "луковица").
_ONION_VARIANTS={'лук','лук репчатый','лук красный','лук белый',
                 'лук зелёный','лук-порей','лук порей',
                 'луковица','луковый порошок'}
INDIVIDUAL_ALLERGENS={
 'лук': _ONION_VARIANTS,
 'лук-порей': _ONION_VARIANTS,
 'луковица': _ONION_VARIANTS,
 'луковый порошок': _ONION_VARIANTS,
 'мёд': {'мёд', 'мед'},
 'мед': {'мёд', 'мед'},
}

# Explicit food-word markers; word boundaries prevent "лук" matching an
# unrelated prefix. These are conservative name-based screens, not a substitute
# for checking the actual package's ingredient list.
ALLERGEN_NAME_MARKERS={
 'молоко':('молоко','молочный','молочная','молочное','йогурт','творог',
           'сыр','сливки','сметана','кефир','ряженка','масло сливочное',
           'сгущенное молоко','сухое молоко','моцарелла','брынза'),
 'яйца':('яйцо','яйца','яиц','яйцами','майонез'),
 'арахис':('арахис','арахисовая паста'),
 'орехи':('орехи','орех','миндаль','фундук','кешью','фисташки',
          'пекан','макадамия'),
 'рыба':('минтай','хек','треска','скумбрия','горбуша','лосось',
         'тунец','сардины','сельдь','рыба','рыбный','рыбная'),
 'пшеница':('пшеница','пшеничная','пшеничный','мука пшеничная',
            'макароны','спагетти','батон','манка'),
 'глютен':('пшеница','пшеничная','пшеничный','рожь','ржаной',
           'ячмень','овес','овес','овсяные','манка','булгур','макароны',
           'спагетти','мука пшеничная'),
 'лук':('лук','лука','лук-порей','луковица','луковый',
        'луковая','луковое'),
 'лук-порей':('лук','лука','лук-порей','луковица','луковый'),
 'луковица':('лук','лука','лук-порей','луковица','луковый'),
 'луковый порошок':('лук','лука','лук-порей','луковица','луковый'),
 'мед':('мед','медовый','медовая'),
}


def _name_has_marker(name,marker):
    value=name.casefold().replace('ё','е')
    word=marker.casefold().replace('ё','е')
    return re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',value) is not None

def _allergen_ingredients(allergen):
    a=allergen.strip().casefold().replace('ё','е')
    groups={k.replace('ё','е'):v for k,v in ALLERGEN_GROUPS.items()}
    singles={k.replace('ё','е'):v for k,v in INDIVIDUAL_ALLERGENS.items()}
    result=groups.get(a)
    if result is None:result=singles.get(a)
    # Known single allergens may be entered instead of a whole group.
    if result is None:
        for members in (*groups.values(), *singles.values()):
            if a in {item.casefold().replace('ё','е') for item in members}:
                result={a}
                break
    if result is None:
        raise MenuInvalid('Неизвестная группа или продукт-аллерген: '+allergen)
    return {n.casefold().replace('ё','е') for n in result}

def validate_allergens(prefs):
    """Reject unsupported restrictions even if no recipe contains the ingredient."""
    return [_allergen_ingredients(a) for a in prefs.get('allergy',())]

def blocked(name,prefs):
    n=name.casefold().replace('ё','е')
    if n in {x.casefold().replace('ё','е') for x in prefs.get('dislike',())}:
        return True
    for original,group in zip(prefs.get('allergy',()),validate_allergens(prefs)):
        if n in group:return True
        key=original.strip().casefold().replace('ё','е')
        if n in _KNOWN_ALLERGEN_PRODUCTS.get(key,()):return True
        markers=ALLERGEN_NAME_MARKERS.get(key)
        if markers is None and key in {x.casefold().replace('ё','е') for x in group}:
            markers=(key,)
        if markers and any(_name_has_marker(n,marker) for marker in markers):return True
    return False

# Only the specific milk conversion is enabled; never assume density for other liquids.
MILK_DENSITY_G_PER_ML=1.03

def _ingredient_price_direct(name,amount,unit,prices):
    n=_key(name)
    if unit in ('г','мл'):
        price=prices.get((n,'кг' if unit=='г' else 'л'))
        if price is not None:return price*amount/1000
    else:
        price=prices.get((n,unit))
        if price is not None:return price*amount
    if n=='молоко' and unit=='г':
        price=prices.get((n,'л'))
        if price is not None:return price*amount/(1000*MILK_DENSITY_G_PER_ML)
    if n=='молоко' and unit=='мл':
        price=prices.get((n,'кг'))
        if price is not None:return price*amount*MILK_DENSITY_G_PER_ML/1000
    return None

def ingredient_price(name,amount,unit,prices):
    """Стоимость ингредиента; при отсутствии прямой цены пробует пересчёт единиц (яйца, масло, кефир)."""
    value=_ingredient_price_direct(name,amount,unit,prices)
    if value is None and unit=='г':
        value=unit_conversions.price_via_conversion(_key(name),amount,prices)
    return value

def validate(menu,products,prefs,people,prices):
    if not isinstance(menu,dict) or not isinstance(menu.get('menu'),dict):raise MenuInvalid('Нет меню')
    validate_allergens(prefs)
    catalog={_key(p['name']):p for p in products}
    total=0.;kcal=0.;missing_prices=set();missing_kcal=set();ingredients={};estimated=set();provenance={}
    for day in DAYS:
        daily=menu['menu'].get(day)
        if not isinstance(daily,dict):raise MenuInvalid('Отсутствует день: '+day)
        for meal in MEALS:
            dishes=daily.get(meal)
            if not isinstance(dishes,list) or not dishes:raise MenuInvalid('Нет приёма пищи')
            for dish in dishes:
                if not isinstance(dish,dict) or not isinstance(dish.get('name'),str) or not isinstance(dish.get('ingredients'),list) or not dish['ingredients']:raise MenuInvalid('Не указан состав блюда')
                for item in dish['ingredients']:
                    if not isinstance(item,dict):raise MenuInvalid('Неверный ингредиент')
                    name=item.get('product');unit=item.get('unit');amount=item.get('amount')
                    if not isinstance(name,str) or _key(name) not in catalog:raise MenuInvalid('Ингредиент вне каталога: '+str(name))
                    if menu_blocked(name,prefs,catalog,blocked):raise MenuInvalid('Ингредиент запрещён или аллерген: '+name)
                    if type(amount) not in (float,int) or not 0<amount<=10000 or unit not in ('г','кг','мл','л','шт'):raise MenuInvalid('Неверное количество или единица')
                    if 'price07_code' in catalog[_key(name)] and unit not in ('г','кг'):
                        raise MenuInvalid('Новый продукт допускается только по массе в граммах: '+name)
                    # No automatic density/portion conversions for pieces or liquids.
                    key=(_key(name),unit)
                    ingredients[key]=ingredients.get(key,0)+amount*people
                    p=catalog[_key(name)]
                    cost=ingredient_price(name,amount,unit,prices)
                    if cost is None:
                        missing_prices.add(name)
                        provenance.setdefault(key,{'product':name,'unit':unit,'quality':'missing'})
                    else:
                        total+=cost*people
                        used_key=unit_conversions.used_price_key(_key(name),unit,prices)
                        source=dict(getattr(prices,'provenance',{}).get(used_key,{}) or {})
                        source.setdefault('quality','planning_assumption' if used_key in getattr(prices,'estimate_keys',set()) else 'unknown')
                        provenance.setdefault(key,{'product':name,'unit':unit,**source})
                        if used_key in getattr(prices,'estimate_keys',set()):
                            estimated.add(name)
                    if unit in ('г','кг'):
                        value=menu_energy(name,amount*(1000 if unit=='кг' else 1),catalog)
                    else:value=None
                    if value is None:missing_kcal.add(name)
                    else:kcal+=value*people
    packs=purchase_estimate(ingredients,prices)
    return {'known_cost':round(total,2),'missing_prices':sorted(missing_prices),
            'estimated_prices':sorted(estimated),
            'purchase_estimate':float(packs['purchase_estimate']),
            'packaging_assumed':False,'missing_purchase_prices':packs['unpriced'],
            'known_kcal':round(kcal,1),'missing_kcal':sorted(missing_kcal),
            'ingredients':ingredients,'price_provenance':list(provenance.values())}

async def build(uid,people,budget,calories,provider=None,diet_mode='regular'):
    from menu_workers import run_heavy
    products,prefs,prices,_=await run_heavy(context,uid)
    from menu_diet import recipe_is_eligible, validate_menu as validate_diet
    validate_allergens(prefs)
    if not products:raise MenuInvalid('Каталог пуст')
    catalog={_key(p['name']):p for p in products}
    # Only data needed for planning is sent; no user ID or personal identifiers.
    # The price catalog covers 258 products; nutritional data is smaller. Do
    # not ask the LLM to build unverifiable calorie targets from unknown foods.
    spec={'days':list(DAYS),'meals':list(MEALS),'people':people,'budget_rub':budget,'kcal_per_person_per_day':calories,
          'diet_mode':diet_mode,
          'products':[{'name':p['name'],'unit':p['unit'],'price_known':(_key(p['name']),p['unit']) in prices}
                      for p in products if menu_energy(p['name'],100,catalog) is not None
                      and not menu_blocked(p['name'],prefs,catalog,blocked)
                      and recipe_is_eligible([(p['name'],100)],diet_mode)],
          'allergies':prefs['allergy'],'dislikes':prefs['dislike'],'likes':prefs['like']}
    messages=[{'role':'system','content':'Ты планировщик домашнего меню, не врач. Верни JSON объект {"menu": {день: {приём пищи: [{"name": название блюда, "ingredients": [{"product": точное название из каталога, "amount": положительное число, "unit": "г" или "кг" или "мл" или "л" или "шт"}]}]}}}. Ровно 7 дней, 3 приёма пищи каждый. Количества ингредиентов указывай НА ОДНОГО человека. Не используй аллергены, нелюбимые и отсутствующие в каталоге продукты. Не утверждай медицинскую безопасность.'},{'role':'user','content':json.dumps(spec,ensure_ascii=False)}]
    last='';needed_seen=[]
    for attempt in range(3):
        try:
            result=await generate_json(messages,provider=provider)
            validate_diet(result,diet_mode)
            stats=await run_heavy(validate,result,products,prefs,people,prices)
            if not stats['missing_prices'] and menu_rules.is_over_budget(stats['purchase_estimate'],budget):
                raise BudgetExceeded('Превышение бюджета более 5%',budget,stats['purchase_estimate'])
            return result,stats
        except (MenuInvalid,LLMError) as exc:
            last=str(exc)
            if isinstance(exc,BudgetExceeded):needed_seen.append(exc.needed)
            messages.append({'role':'user','content':'Исправь меню полностью. Ошибка проверки: '+last+'. Верни полный корректный JSON.'})
    if needed_seen and last.startswith('Превышение бюджета'):
        # Последняя попытка не уложилась в допуск: возвращаем структурированный отказ, чтобы интерфейс предложил пути выхода.
        best=min(needed_seen)
        raise BudgetExceeded(f'Бюджет {budget:g} ₽: внешний генератор за 3 попытки не уложился в допуск '
                             f'+{menu_rules.BUDGET_TOLERANCE*100:.0f}% (лучшая закупка около {best:.0f} ₽).',budget,best)
    hint=f' Ранее попытки превышали бюджет (лучшая закупка около {min(needed_seen):.0f} ₽).' if needed_seen else ''
    raise MenuInvalid('Не удалось сформировать проверенное меню: '+last+hint)

