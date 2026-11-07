"""Deterministic editorial shortlist from existing recipes, not an approval path.

The returned 100 entries remain *candidates* until each is independently
verified against ingredient, cooking, nutrition and allergen evidence.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict

TARGETS={'Завтрак':30,'Обед':40,'Ужин':30}


def _key(name):
    return ' '.join(str(name).lower().replace('ё','е').split())


def _ingredients(raw):
    try:
        items=json.loads(raw)
        if not isinstance(items,list) or not items:
            return ()
        result=[]
        for item in items:
            if not isinstance(item,dict) or not item.get('product') or item.get('unit') not in ('г','кг'):
                return ()
            amount=item.get('amount')
            if (type(amount) not in (int,float) or not math.isfinite(amount) or amount<=0):
                return ()
            result.append(_key(item['product']))
        return tuple(sorted(set(result)))
    except (ValueError,TypeError):
        return ()


def select_editorial_pilot(db, targets=None):
    """Spread candidates among meal categories and distinct ingredient sets.

    Sorting and ties are stable, avoiding dependence on SQLite row insertion
    order. No writes, no claims of verified nutrient data.
    """
    quotas=TARGETS if targets is None else targets
    if not quotas or any(type(n) is not int or n<0 for n in quotas.values()):
        raise ValueError('Invalid meal targets')
    rows=db.execute('''SELECT id,title,category,meal,ingredients_json,preparation,
                             nutrition_status FROM zr_recipes WHERE active=1
                      ORDER BY id''').fetchall()
    groups=defaultdict(list)
    for rid,title,category,meal,raw,preparation,status in rows:
        ingredients=_ingredients(raw)
        if meal in quotas and ingredients and preparation and title:
            groups[meal].append({'recipe_id':rid,'title':title,'category':category,
                                 'meal':meal,'ingredients':ingredients,
                                 'source_status':status})
    selected=[]
    for meal,quota in quotas.items():
        remain=list(groups[meal])
        if len(remain)<quota:
            raise ValueError(f'Not enough complete candidates for {meal}')
        category_counts=Counter()
        ingredient_counts=Counter()
        set_counts=Counter()
        for _ in range(quota):
            def penalty(r):
                # Diverse category coverage, then ingredient identities.
                return (category_counts[r['category']]*3
                        + set_counts[r['ingredients']]*8
                        + sum(ingredient_counts[n] for n in r['ingredients'])/len(r['ingredients']),
                        _key(r['title']),r['recipe_id'])
            item=min(remain,key=penalty)
            remain.remove(item)
            category_counts[item['category']]+=1
            set_counts[item['ingredients']]+=1
            ingredient_counts.update(item['ingredients'])
            selected.append({'recipe_id':item['recipe_id'],'title':item['title'],
                             'category':item['category'],'meal':item['meal'],
                             'ingredient_names':item['ingredients'],
                             'component_role':('salad' if item['category']=='Салаты' else
                                               'side' if item['category']=='Овощи и гарниры' else
                                               'main'),
                             'review_status':'needs_editorial_review',
                             'nutrition_status':'unverified',
                             'reason':'Balanced editorial candidate; never automatically approved'})
    return tuple(selected)
