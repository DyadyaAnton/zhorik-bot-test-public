"""Offline weekly menu planner. Recipe amounts are per person; no synthetic prices.

Recipes are TEST fixtures, not clinically validated nutritional plans.
"""
import asyncio
import random
import json
import math
import time
import sqlite3
from collections import Counter
from database import connect_db
from menu_generator import DAYS, MEALS, MenuInvalid, BudgetExceeded, blocked, validate, context, ingredient_price, validate_allergens
import menu_rules
from calorie_catalog import energy
from menu_diet import get as diet_get, recipe_is_eligible, validate_menu as validate_diet
from menu_product_identity import key as ingredient_key, repetition_penalty, load_codes

# Each recipe uses canonical catalog product names and gram amounts per person.
_RECIPES = {
 'Завтрак': [
 ('Овсянка с яблоком', [('Овсяные хлопья',95),('Яблоки',160)]),
 ('Овсянка с бананом', [('Овсяные хлопья',95),('Бананы',140)]),
 ('Гречка с яблоком', [('Гречка',100),('Яблоки',150)]),
 ('Рисовая каша с яблоком', [('Рис',95),('Яблоки',150)]),
 ('Творог с бананом', [('Творог',250),('Бананы',120)]),
 ('Творог с яблоком', [('Творог',250),('Яблоки',150)]),
 ('Бутерброд с сыром', [('Хлеб ржаной',130),('Сыр твердый',65)]),
 ('Овсянка с молоком', [('Овсяные хлопья',100),('Молоко',200)]),
 ('Рис с бананом', [('Рис',100),('Бананы',130)]),
 ('Гречка с творогом', [('Гречка',95),('Творог',160)]),
 ('Хлеб с творогом', [('Хлеб ржаной',130),('Творог',200)]),
 ('Картофель с сыром', [('Картофель',300),('Сыр твердый',60)]),
 ],
 'Обед': [
 ('Курица с гречкой', [('Куриное филе',220),('Гречка',110),('Морковь',100)]),
 ('Курица с рисом', [('Куриное филе',220),('Рис',110),('Лук',70)]),
 ('Курица с картофелем', [('Куриное филе',240),('Картофель',380),('Морковь',100)]),
 ('Говядина с гречкой', [('Говядина',200),('Гречка',110),('Лук',70)]),
 ('Говядина с рисом', [('Говядина',200),('Рис',110),('Морковь',100)]),
 ('Свинина с картофелем', [('Свинина',190),('Картофель',350),('Лук',80)]),
 ('Курица с макаронами', [('Куриное филе',210),('Макароны',120),('Помидоры',150)]),
 ('Говядина с макаронами', [('Говядина',200),('Макароны',115),('Лук',80)]),
 ('Тушёная капуста с курицей', [('Капуста белокочанная',350),('Куриное филе',230),('Картофель',200)]),
 ('Рис с курицей и овощами', [('Рис',115),('Куриное филе',220),('Морковь',130)]),
 ('Гречка с курицей и капустой', [('Гречка',115),('Куриное филе',210),('Капуста белокочанная',180)]),
 ('Картофель с говядиной', [('Картофель',380),('Говядина',210),('Лук',80)]),
 ],
 'Ужин': [
 ('Гречка с овощами', [('Гречка',105),('Морковь',150),('Капуста белокочанная',180)]),
 ('Рис с овощами', [('Рис',110),('Морковь',140),('Помидоры',180)]),
 ('Курица с капустой', [('Куриное филе',210),('Капуста белокочанная',350),('Картофель',180)]),
 ('Макароны с сыром', [('Макароны',120),('Сыр твердый',70),('Помидоры',150)]),
 ('Картофель с творогом', [('Картофель',350),('Творог',220)]),
 ('Гречка с курицей', [('Гречка',95),('Куриное филе',190),('Огурцы',180)]),
 ('Рис с курицей', [('Рис',100),('Куриное филе',190),('Помидоры',180)]),
 ('Творог с хлебом', [('Творог',280),('Хлеб ржаной',140)]),
 ('Капуста с говядиной', [('Капуста белокочанная',350),('Говядина',210),('Картофель',180)]),
 ('Картофель с курицей', [('Картофель',350),('Куриное филе',200),('Огурцы',150)]),
 ('Макароны с курицей', [('Макароны',110),('Куриное филе',200),('Помидоры',170)]),
 ('Рис с говядиной', [('Рис',100),('Говядина',190),('Морковь',150)]),
 ],
}

MIN_VARIANT_KCAL=150  # a user's side dish (salad etc.) cannot be a whole meal

def recipes():
    return _RECIPES


def _breakfast_family(title):
    title=title.casefold()
    if any(x in title for x in ('каша','овсянка','гречнев','рисовая')):return 'каша'
    if any(x in title for x in ('блины','блинчики','оладьи','гренки')):return 'мучной завтрак'
    if any(x in title for x in ('бутерброд','хлеб с')):return 'бутерброд'
    if 'творог' in title:return 'творог'
    return 'другое'

def _build_recipe_pool(uid, diet_mode, products, prefs, prices):
    """Prepare eligible recipes once per generation; no cross-user caching."""
    catalog={p['name'].casefold() for p in products}
    from recipe01_legacy_quality import safe_for_new_generation
    pools={}
    # Bring in the expandable recipe bank without replacing the 36 legacy
    # stable recipes. Favorite IDs receive a modest scoring preference.
    # Legacy combined porridges remain available as old recipe records and favorites,
    # but should not bypass the new base-plus-addons constructor in generated menus.
    def _legacy_porridge(title):
        label=title.casefold()
        return any(x in label for x in ('овсянк', 'каша', 'гречка с яблоком', 'рис с бананом'))
    expanded={meal:[row for row in rows if not (meal=='Завтрак' and _legacy_porridge(row[0]))]
              for meal,rows in _RECIPES.items()}
    favorites=set()
    favorite_names=set()
    with connect_db() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='zr_recipes'").fetchone():
            favorites={r[0] for r in db.execute("SELECT recipe_id FROM zr_favorites WHERE user_id=?",(uid,))}
            if favorites:
                favorite_names={r[0] for r in db.execute(
                    'SELECT title FROM zr_recipes WHERE id IN ('+','.join('?' for _ in favorites)+')',
                    tuple(favorites))}
            for row in db.execute("SELECT id,title,meal,ingredients_json FROM zr_recipes WHERE active=1"):
                if row['meal'] not in expanded:continue
                # Quarantine contradictory legacy templates from new menus;
                # preserve their IDs, favorites and historical snapshots.
                if not safe_for_new_generation(row['title'], '', '', row['ingredients_json']):continue
                parts=json.loads(row['ingredients_json'])
                if not all(p['unit']=='г' for p in parts):continue
                expanded[row['meal']].append((row['title'],[(p['product'],p['amount']) for p in parts]))
    # Personal recipes are private immutable snapshots; validate with the same filters.
    with connect_db() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zm_recipe_variants'").fetchone():
            seen_variants=set()
            for row in db.execute("SELECT id,title,kind,recipe_json FROM zm_recipe_variants WHERE owner_id=? ORDER BY id",(uid,)):
                try:
                    variant=json.loads(row['recipe_json'])
                    parts=variant['ingredients']
                    if not parts or any(i['unit']!='г' or float(i['amount'])<=0 for i in parts):continue
                    # Identical copies (e.g. saved by repeated taps) must not multiply a favorite's weight.
                    signature=(row['kind'],tuple(sorted((i['product'],float(i['amount'])) for i in parts)))
                    if signature in seen_variants:continue
                    seen_variants.add(signature)
                    energies=[energy(i['product'],float(i['amount'])) for i in parts]
                    if any(e is None for e in energies) or sum(energies)<MIN_VARIANT_KCAL:continue
                    title=row['title']+' · мой вариант №'+str(row['id'])
                    expanded['Завтрак' if row['kind'] in ('porridge','curd') else 'Обед' if row['kind']=='soup' else 'Ужин' if row['kind'] in ('vegetable','fruit') else 'Обед'].append((title,[(i['product'],i['amount']) for i in parts]))
                    favorite_names.add(title)
                except (ValueError,TypeError,KeyError):
                    continue
    for meal,recipes_for_meal in expanded.items():
        available=[]
        for name,parts in recipes_for_meal:
            if not recipe_is_eligible(parts,diet_mode,title=name):continue
            if any(n.casefold() not in catalog or blocked(n,prefs) for n,_ in parts):continue
            kcal=sum(energy(n,g) or 0 for n,g in parts)
            missing_kcal=any(energy(n,g) is None for n,g in parts)
            cost=0;missing_price=False
            for n,g in parts:
                amount_cost=ingredient_price(n,g,'г',prices)
                if amount_cost is None:missing_price=True
                else:cost+=amount_cost
            if missing_kcal:
                continue  # Unknown nutrition cannot be optimized or compared to a target.
            available.append((name,parts,kcal,missing_kcal,cost,missing_price))
        if not available:raise MenuInvalid('Нет рецептов с известной калорийностью для '+meal+' с учётом ограничений.')
        pools[meal]=available
    return pools, favorite_names


# Мясные продукты для режима «меньше мяса» (рыба и яйца к мясу не относятся).
# Названия в каталоге бывают и «старые» (Говядина), и канонические (Говядина мякоть, Куриная грудка), поэтому сравниваем по основам слов.
_MEAT_STEMS = ('говя','говяж','свин','кури','индей','индюш','баран','телят','кролик','утк','гус','колбас','сосис','ветчин','бекон','фарш')
_MEAT_WORDS = {'печень','утка','гусь','мясо'}
_NOT_MEAT_HINTS = ('рыб','сое','тофу')  # «Рыбный фарш», «Соевое мясо», тофу — не мясо


def _is_meat_name(name):
    import re
    text=name.casefold().replace('ё','е')
    if any(h in text for h in _NOT_MEAT_HINTS):return False
    words=re.findall(r'[а-я]+',text)
    return any(w in _MEAT_WORDS or any(w.startswith(stem) for stem in _MEAT_STEMS) for w in words)


def _is_meat(parts):
    return any(_is_meat_name(n) for n,_ in parts)


def _build_one(uid, people, budget, calories, variation=0, diet_mode='regular', prepared_context=None, prepared_pool=None, meat_cap=None, prepared_codes=None, validate_initial=True, prepared_features=None):
    products,prefs,prices,_=prepared_context if prepared_context is not None else context(uid)
    validate_allergens(prefs)
    if prepared_pool is None:
        prepared_pool=_build_recipe_pool(uid,diet_mode,products,prefs,prices)
    pools, favorite_names=prepared_pool
    # Upper bound on reachable daily energy with controlled portion scaling.
    MAX_PORTION_FACTOR=1.5
    min_daily=sum(min(r[2] for r in pools[meal]) for meal in MEALS)
    if calories < min_daily / 1.1:
        raise MenuInvalid(f'Цель {calories:g} ккал/сутки недостижима: минимум примерно {min_daily:.0f} ккал с текущими рецептами и ограничениями.')
    max_daily=sum(max(r[2] for r in pools[meal]) for meal in MEALS)*MAX_PORTION_FACTOR
    if calories>max_daily*1.1:
        raise MenuInvalid(f'Цель {calories:g} ккал/сутки недостижима: максимум примерно {max_daily:.0f} ккал с текущими рецептами и ограничениями.')
    # Never use unknown kcal as zero to claim a target was met.
    rng=random.Random(variation)
    menu={'menu':{}}
    counts={}
    protein_counts={}
    staple_counts={}
    ingredient_counts={}
    codebook=prepared_codes if prepared_codes is not None else load_codes()
    meat_meals=0  # приёмов с мясом за неделю (для meat_cap)
    proteins={'Куриное филе','Говядина','Свинина','Индейка филе',
              'Минтай','Треска','Горбуша','Хек','Скумбрия'}
    staples={'Рис','Гречка','Макароны','Картофель','Овсяные хлопья'}
    # These identities depend on the recipe and catalog, not on the day or
    # trial score. Canonicalization is expensive and was repeated hundreds of
    # thousands of times in a single 24-basket search.
    recipe_features=prepared_features if prepared_features is not None else {id(rec):(
        tuple(ingredient_key(n,codebook) for n,_ in rec[1]),
        _is_meat(rec[1])) for meal_rows in pools.values() for rec in meal_rows}
    penalty_cache={}
    def repeat_cost(name, previous):
        cache_key=(name,previous)
        if cache_key not in penalty_cache:
            penalty_cache[cache_key]=repetition_penalty(name,previous)
        return penalty_cache[cache_key]
    # Owner's preferred distribution is a SOFT scoring target, not a cutoff.
    role_shares={'Завтрак':0.27,'Обед':0.38,'Ужин':0.35}
    for day in DAYS:
        daily={};day_kcal=0;day_cost=0
        day_proteins=set()
        day_staples=set()
        day_names=set()
        for idx,meal in enumerate(MEALS):
            choices=[]
            for rec in pools[meal]:
                name,parts,kcal,missing_kcal,cost,missing_price=rec
                if name in day_names:continue
                canonical_parts,is_meat=recipe_features[id(rec)]
                if meat_cap is not None and meat_meals>=meat_cap and is_meat:continue
                remaining=3-idx
                remaining_goal=max(0,(calories-day_kcal)/remaining)
                kcal_goal=max(0,calories*role_shares[meal]*0.70+remaining_goal*0.30)
                cost_goal=max(0,(budget/7/people-day_cost)/remaining)
                # Missing prices/kcal incur uncertainty penalties; never substitute made-up values.
                score=abs(kcal-kcal_goal)/max(300,kcal_goal)
                score+=(max(0,cost-cost_goal)/max(1,cost_goal) if not missing_price else 1.5)
                # Prefer realistic ingredient portions when energy/cost scores are similar.
                score+=max(0, max(g for _,g in parts)-350)/1000
                # Variations explore materially different baskets, not tiny
                # cosmetic reorderings of the same expensive menu.
                score+=counts.get(name,0)*0.55+rng.random()*0.55
                # Repeating one protein or grain all week is a real UX problem
                # even when all calories and budget arithmetic are valid.
                score+=sum(protein_counts.get(n,0)*0.11 for n,_ in parts if n in proteins)
                score+=sum(staple_counts.get(n,0)*0.06 for n,_ in parts if n in staples)
                # Penalize identical staples/proteins twice on one day,
                # and ingredients already dominating the weekly menu.
                score+=sum(0.75 for n,_ in parts if n in day_staples)
                score+=sum(0.60 for n,_ in parts if n in day_proteins)
                score+=sum(max(0,staple_counts.get(n,0)-3)*0.22 for n,_ in parts if n in staples)
                score+=sum(max(0,protein_counts.get(n,0)-3)*0.20 for n,_ in parts if n in proteins)
                # The same ingredient can dominate a week even when every
                # dish title differs (e.g. cottage cheese in six recipes).
                # Soft penalties preserve feasibility for restricted diets.
                for (n,_),canonical in zip(parts,canonical_parts):
                    previous=ingredient_counts.get(canonical,0)
                    # Restricted diets have fewer eligible options; diversity
                    # must not displace calorie feasibility.
                    score+=repeat_cost(n,previous)*(0.0 if diet_mode=='gluten_avoid' else 1.0)
                if diet_mode=='pp':
                    if any(n in {'Свинина','Масло сливочное'} for n,_ in parts):score+=0.45
                    if any(n in {'Минтай','Треска','Индейка филе','Куриное филе'} for n,_ in parts):score-=0.22
                    if meal=='Ужин' and any(n=='Макароны' for n,_ in parts):score+=0.45
                    if 'двойным сыром' in name.casefold():score+=0.7
                if name in favorite_names:
                    score-=0.60 if not counts.get(name,0) else 0.12
                choices.append((score,rec))
            # Favorites have a soft preference, never override calorie/budget.
            if not choices:
                raise MenuInvalid('Не хватает различных рецептов для '+meal+' при выбранных ограничениях.')
            _,selected=min(choices,key=lambda x:x[0]);name,parts,kcal,missing_kcal,cost,missing_price=selected
            counts[name]=counts.get(name,0)+1
            day_names.add(name)
            selected_parts,is_meat=recipe_features[id(selected)]
            if is_meat:meat_meals+=1
            for (n,_),canonical in zip(parts,selected_parts):
                ingredient_counts[canonical]=ingredient_counts.get(canonical,0)+1
                if n in proteins:protein_counts[n]=protein_counts.get(n,0)+1
                if n in staples:staple_counts[n]=staple_counts.get(n,0)+1
                if n in proteins:day_proteins.add(n)
                if n in staples:day_staples.add(n)
            day_kcal+=kcal;day_cost+=cost
            daily[meal]=[{'name':name,'ingredients':[{'product':n,'amount':g,'unit':'г'} for n,g in parts]}]
        # Increase portions conservatively; no artificial calorie entries.
        # REV7: deliberately leave ~8% energy headroom for extra components.
        # Finalization still enforces the actual ±10% daily target and +5% budget.
        factor=min(MAX_PORTION_FACTOR,max(1.0,calories*0.92/day_kcal)) if day_kcal>0 else 1.0
        if factor>1:
            for dishes in daily.values():
                for dish in dishes:
                    for item in dish['ingredients']:
                        item['amount']=round(item['amount']*factor,2)
        menu['menu'][day]=daily
    # The multi-basket caller validates the rounded menu immediately after
    # construction. Avoid a second full scan of the unrounded, discarded
    # version; standalone callers retain the original validation contract.
    if not validate_initial:
        return menu,None
    stats=validate(menu,products,prefs,people,prices)
    stats['mode']='local'
    stats['target_verified']=not stats['missing_kcal'] and abs(stats['known_kcal']/people/7-calories)<=calories*.1
    # A complete estimate is still not a *verified* budget when prices and
    # pack sizes are provisional. Show approximate cost and overshoot instead.
    stats['budget_verified']=False
    stats['recipes_count']=sum(map(len,pools.values()))
    if not stats['target_verified'] and not stats['missing_kcal']:
        actual=stats['known_kcal']/people/7
        direction='минимум' if actual>calories else 'максимум'
        raise MenuInvalid(f'Цель {calories:g} ккал/сутки недостижима при текущем подборе: {direction} около {actual:.0f} ккал. Попробуйте изменить цель или набор рецептов.')
    return menu,stats


MAX_BASKET_TRIALS=24


def _diversity_penalty(menu):
    """Rank otherwise feasible baskets; never substitutes for hard validation.

    All names are taken from already validated recipe records.  This score is
    about balance of a *week*, not about nutritional safety of any one food.
    """
    titles=Counter()
    products=Counter()
    breakfast_families=Counter()
    dairy_meals=Counter()
    weak_dinners=0
    vegetable_free_days=0
    vegetables={'морковь','капуста белокочанная','огурцы','помидоры',
                'перец сладкий','свекла','кабачки','лук','брокколи'}
    dairy={'творог','сыр твердый','молоко','сметана','сливки','йогурт'}
    staples={'гречка','рис','макароны','картофель','овсяные хлопья',
             'хлеб ржаной','хлеб белый'}
    for day in DAYS:
        seen_veg=False
        for meal in MEALS:
            for dish in menu['menu'][day][meal]:
                title=dish['name'].casefold()
                titles[title]+=1
                parts=dish['ingredients']
                names={i['product'].casefold().replace('ё','е') for i in parts}
                products.update(names)
                if names & vegetables:seen_veg=True
                if names & dairy:dairy_meals[meal]+=1
                if meal=='Ужин' and len(names)<=2:weak_dinners+=1
                if meal=='Завтрак':
                    family=_breakfast_family(title)
                    breakfast_families[family]+=1
        if not seen_veg:vegetable_free_days+=1
    return (sum(max(0,n-1)*6 for n in titles.values())
            +sum(max(0,n-4)*1.0 for p,n in products.items() if p in staples)
            +sum(max(0,n-4)*1.3 for p,n in products.items() if p in dairy)
            +sum(max(0,n-3)*1.6 for fam,n in breakfast_families.items() if fam!='другое')
            +max(0,dairy_meals['Ужин']-1)*2.1
            +weak_dinners*1.5 + vegetable_free_days*1.4)


def _rank_feasible_baskets(candidates, budget):
    """Sort validated (cost, menu, stats) options, preserving budget rules."""
    feasible=[v for v in candidates if v[0]<=menu_rules.budget_limit(budget)]
    if not feasible:
        return []
    affordable=[v for v in feasible if v[0]<=budget+0.005]
    if affordable:
        feasible=affordable
    else:
        floor=min(v[0] for v in feasible)
        feasible=[v for v in feasible if v[0]<=floor*1.03+0.005]
    return sorted(feasible,key=lambda x:(_diversity_penalty(x[1]),x[0]))


def _round_grams_for_quote(menu):
    """Match menu_ui's final rounding before evaluating the real basket."""
    for day in menu['menu'].values():
        for dishes in day.values():
            for dish in dishes:
                for item in dish['ingredients']:
                    if item['unit']=='г':
                        item['amount']=int(math.ceil((item['amount']-1e-9)/10)*10)


def _build(uid, people, budget, calories, variation=0, diet_mode=None, meat_cap=None):
    """Choose a feasible consumption-priced basket, or report an honest failure.

    The same rounded amounts are used for comparison and final display.
    Provisional prices can make a *planning* budget feasible, not verified.
    """
    best=None
    diet_mode=diet_mode or diet_get(uid)
    best_cost=float('inf')
    candidates={}
    last_error=None
    # Snapshot catalog, preferences and prices once for the entire search.
    # Re-fetching on each trial is expensive and can mix different price snapshots.
    from perf_diagnostics import mark
    prep_started=time.perf_counter()
    prepared_context=context(uid)
    products,prefs,prices,_=prepared_context
    prepared_pool=_build_recipe_pool(uid,diet_mode,products,prefs,prices)
    prepared_codes=load_codes()
    prepared_features={id(rec):(
        tuple(ingredient_key(n,prepared_codes) for n,_ in rec[1]),
        _is_meat(rec[1])) for rows in prepared_pool[0].values() for rec in rows}
    mark('menu_build_prepare',time.perf_counter()-prep_started)
    search_started=time.perf_counter()
    # Keep the core search identical on subsequent requests: changing the
    # variation must not turn an already affordable basket into a refusal.
    for trial in range(MAX_BASKET_TRIALS):
        try:
            menu,_=_build_one(uid,people,budget,calories,variation=trial,diet_mode=diet_mode,prepared_context=prepared_context,prepared_pool=prepared_pool,meat_cap=meat_cap,prepared_codes=prepared_codes,validate_initial=False,prepared_features=prepared_features)
            _round_grams_for_quote(menu)
            validate_diet(menu,diet_mode)
            stats=validate(menu,products,prefs,people,prices)
            if stats['missing_prices'] or stats['missing_purchase_prices'] or stats['missing_kcal']:
                continue
            daily_kcal=stats['known_kcal']/people/7
            if abs(daily_kcal-calories)>calories*.1:
                continue
            cost=stats['purchase_estimate']
            # A real regenerate request should select another valid combination, not
            # always the cheapest identical outcome. Deduplicate by dish titles.
            signature=tuple(tuple(tuple(d['name'] for d in menu['menu'][day][meal])
                                  for meal in MEALS) for day in DAYS)
            if signature not in candidates or cost<candidates[signature][0]:
                candidates[signature]=(cost,menu,stats)
            if cost<best_cost:
                best=(menu,stats)
                best_cost=cost
        except MenuInvalid as exc:
            last_error=exc
            continue
    mark('menu_build_trials',time.perf_counter()-search_started)
    if best is None:
        if last_error is not None:raise last_error
        raise MenuInvalid('Не удалось собрать меню с полной ценой и подтверждённым расчётом калорийности.')
    if menu_rules.is_over_budget(best_cost,budget):
        raise BudgetExceeded(f'Бюджет {budget:.0f} ₽: даже самый экономный из проверенных вариантов '
                             f'потребует ориентировочно {best_cost:.2f} ₽ (по фактическому расходу продуктов, без округления до упаковок) '
                             f'(+{(best_cost/budget-1)*100:.1f}%, допуск +{menu_rules.BUDGET_TOLERANCE*100:.0f}%). '
                             'Увеличьте бюджет либо измените условия; меню не сохранено.',budget,best_cost)
    feasible=_rank_feasible_baskets(candidates.values(),budget)
    if feasible:
        # Read-only comparison with recent saved weeks. Avoid returning an
        # identical main-dish schedule when another validated basket exists.
        previous=set()
        try:
            with connect_db() as db:
                # Best-effort diversity only; a busy history must not prevent
                # delivery of an otherwise validated affordable menu.
                db.execute('PRAGMA busy_timeout=250')
                for row in db.execute('SELECT result_json FROM menus WHERE owner_id=? '
                                      'ORDER BY id DESC LIMIT 8',(uid,)):
                    try:
                        old=json.loads(row['result_json'])['menu']['menu']
                        signature=tuple(tuple(tuple(d['name'] for d in old[day][meal][:1])
                                              for meal in MEALS) for day in DAYS)
                        previous.add(signature)
                    except (KeyError,ValueError,TypeError,IndexError):
                        continue
        except sqlite3.Error:
            previous.clear()
        def titles_signature(option):
            return tuple(tuple(tuple(d['name'] for d in option[1]['menu'][day][meal][:1])
                               for meal in MEALS) for day in DAYS)
        unseen=[option for option in feasible if titles_signature(option) not in previous]
        choices=unseen or feasible
        # Keep affordable, quality-ranked selection and vary between requests.
        selected=choices[max(0,int(variation)) % len(choices)]
        best_cost,menu,stats=selected
    else:
        menu,stats=best
    stats['mode']='local'
    stats['target_verified']=True
    stats['budget_verified']=False
    stats['budget_within_tolerance']=True
    stats['diet_mode']=diet_mode
    return menu,stats,prepared_context

async def build(uid,people,budget,calories,variation=0,diet_mode=None,meat_cap=None):
    from menu_workers import run_heavy
    return await run_heavy(_build,uid,people,budget,calories,variation,diet_mode,meat_cap)

