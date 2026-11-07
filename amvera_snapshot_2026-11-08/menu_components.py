"""Conservative multi-dish enrichment of a generated 7-day menu.

Each component has real catalog ingredients; calories/prices are ALWAYS
revalidated by menu_generator.validate and planning_prices.purchase_estimate.
Unknown kcal are never turned into zeros. No recipes are persisted here.
"""
from copy import deepcopy
from collections import Counter
from calorie_catalog import energy
from menu_generator import DAYS, MEALS, blocked, validate
from menu_diet import recipe_is_eligible, validate_menu
import menu_rules
from menu_product_identity import key as ingredient_key, repetition_penalty, load_codes

# Quantities are grams per person. Water for cooking is not a purchased ingredient.
COMPONENTS={
    'Завтрак': [('Яблоко к завтраку', [('Яблоки',100)]),
                ('Груша к завтраку', [('Груши',110)]),
                ('Банан к завтраку', [('Бананы',90)]),
                ('Ягоды к завтраку', [('Ягоды замороженные',100)]),
                ('Йогурт к завтраку', [('Йогурт',100)]),
                ('Творог с яблоком', [('Творог',60),('Яблоки',60)])],
    'Обед': [('Овощной суп', [('Картофель',55),('Морковь',35),('Лук',10)]),
             ('Салат из огурцов и помидоров', [('Огурцы',55),('Помидоры',55)]),
             ('Капустный салат', [('Капуста белокочанная',90),('Морковь',35)]),
             ('Морковный салат', [('Морковь',110)]),
             ('Салат из помидоров с луком', [('Помидоры',100),('Лук',20)]),
             ('Огурцы с капустой', [('Огурцы',65),('Капуста белокочанная',65)])],
    'Ужин': [('Свежие овощи', [('Огурцы',60),('Помидоры',60)]),
             ('Салат из капусты', [('Капуста белокочанная',90),('Морковь',30)]),
             ('Морковь с огурцом', [('Морковь',70),('Огурцы',70)]),
             ('Томатный салат', [('Помидоры',110),('Лук',20)]),
             ('Капуста с огурцом', [('Капуста белокочанная',65),('Огурцы',65)])],
}

def _kcal(dish):
    total=0.
    for i in dish['ingredients']:
        if i['unit']!='г':return None
        val=energy(i['product'],i['amount'])
        if val is None:return None
        total+=val
    return total


def expand(menu, stats, products, prefs, people, prices, diet_mode='regular', budget=None):
    """Enrich each meal independently; one ineligible side must not drop all sides.

    Every accepted addition is validated against allergens, diet, kcal and
    full-basket cost. The final UI validator also runs after rounding.
    """
    catalog={p['name'].casefold() for p in products}
    codes=load_codes()
    candidate=deepcopy(menu)
    accepted=0
    # Track all accepted sides and their ingredients, not only dish titles:
    # otherwise the same cottage-cheese snack can recur most mornings.
    side_titles=Counter()
    side_ingredients=Counter()
    # Count main dish ingredients as well: an apple in porridge and an apple
    # side on the same day must count as TWO uses of one canonical product.
    all_ingredients=Counter()
    for meals in candidate['menu'].values():
        for dishes in meals.values():
            for dish in dishes:
                all_ingredients.update({ingredient_key(i['product'],codes) for i in dish['ingredients']})
    for day_index,day in enumerate(DAYS):
        for meal in MEALS:
            originals=candidate['menu'][day][meal]
            if len(originals)>=3:
                continue
            before_values=[_kcal(d) for d in originals]
            if any(v is None for v in before_values):
                continue
            before=sum(before_values)
            if before<=0:
                continue
            options=COMPONENTS[meal]
            current_day={ingredient_key(i['product'],codes)
                         for dishes in candidate['menu'][day].values()
                         for dish in dishes for i in dish['ingredients']}
            def side_score(index,entry):
                title,parts=entry
                names={ingredient_key(n,codes) for n,_ in parts}
                # Maintain the day rotation only as a small tie breaker.
                rotation=(index-day_index)%len(options)
                # Prefer new side dishes across the week; keep a soft penalty
                # so a restricted diet/budget can still reuse an eligible side.
                score=side_titles[title]*4.0+rotation*0.15
                score+=sum(side_ingredients[n]*0.16 for n in names)
                score+=sum(repetition_penalty(n,all_ingredients[ingredient_key(n,codes)])*1.6 for n,_ in parts)
                score+=sum(4.0 for n in names if n in current_day)
                if 'творог' in names:
                    existing=sum(1 for meals in candidate['menu'].values()
                                 for dishes in meals.values() for dish in dishes
                                 if any(i['product'].casefold()=='творог' for i in dish['ingredients']))
                    score+=max(0,existing-2)*0.75
                # A modest default preference for soup at lunch, never an
                # override of allergies, diet, energy or affordability.
                if meal=='Обед' and 'суп' in title.casefold() and side_titles[title]<3:
                    score-=0.9
                return score
            ordered=sorted(enumerate(options),key=lambda pair:side_score(*pair))
            for _,(title,parts) in ordered:
                if any(n.casefold() not in catalog or blocked(n,prefs) for n,_ in parts):
                    continue
                if not recipe_is_eligible(parts,diet_mode,title=title):
                    continue
                added={'name':title,'ingredients':[
                    {'product':n,'amount':g,'unit':'г'} for n,g in parts]}
                extra=_kcal(added)
                if extra is None or not 0<extra<before*.35:
                    continue
                trial=deepcopy(candidate)
                dishes=trial['menu'][day][meal]
                factor=(before-extra)/before
                for dish in dishes:
                    for item in dish['ingredients']:
                        item['amount']=round(item['amount']*factor,2)
                dishes.append(added)
                try:
                    validate_menu(trial,diet_mode)
                    calculated=validate(trial,products,prefs,people,prices)
                except (ValueError,TypeError,KeyError):
                    continue
                if calculated['missing_kcal'] or calculated['missing_prices'] or calculated['missing_purchase_prices']:
                    continue
                if budget and menu_rules.is_over_budget(calculated['purchase_estimate'],budget):
                    continue
                candidate=trial
                side_titles[title]+=1
                accepted_keys={ingredient_key(n,codes) for n,_ in parts}
                side_ingredients.update(accepted_keys)
                all_ingredients.update(accepted_keys)
                accepted+=1
                break
    return candidate,accepted>0

