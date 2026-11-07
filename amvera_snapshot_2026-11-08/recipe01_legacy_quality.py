"""Read-only quality flags for inherited template recipes.

Never deletes old IDs or favorites, and never equates a cosmetic name fix with
culinary or nutritional verification.
"""
from __future__ import annotations

import json
import math
import re

REDUNDANT_ROOTS=('картоф','морков','куриц','греч','рисов','творог','огур',
                 'капуст','томат','сыр','яйц')
SIDE_CATEGORIES={'Овощи и гарниры','Салаты'}


def quality_flags(title, category, preparation, ingredients_json):
    flags=[]
    title_key=title.casefold().replace('ё','е')
    words=re.findall(r'[а-я]+',title_key)
    repeated=[root for root in REDUNDANT_ROOTS if sum(root in w for w in words)>=2]
    if repeated:flags.append('redundant_title')
    if category in SIDE_CATEGORIES:flags.append('component_not_full_meal')
    if 'предварительн' in preparation.casefold():flags.append('unverified_cooking_method')
    try:
        ingredients=json.loads(ingredients_json)
        if not isinstance(ingredients,list) or not ingredients:raise ValueError()
        if any(not isinstance(i,dict) or not i.get('product') or
               type(i.get('amount')) not in (int,float) or not math.isfinite(i['amount']) or i['amount']<=0 or
               i.get('unit') not in ('г','кг') for i in ingredients):
            flags.append('invalid_ingredient_quantity')
    except (TypeError,ValueError):
        flags.append('invalid_ingredients_json')
    return tuple(flags)


def safe_for_new_generation(title, category, preparation, ingredients_json):
    """Quarantine impossible template combinations, without deleting history.

    Being allowed here is *not* a nutrition verification or editorial approval.
    The broad legacy pool remains subject to its existing eligibility filters.
    """
    flags = quality_flags(title, category, preparation, ingredients_json)
    return not any(flag in flags for flag in (
        'redundant_title', 'invalid_ingredients_json', 'invalid_ingredient_quantity'))


def audit_legacy_recipes(db):
    """Inventory weaknesses without modifying working records."""
    rows=db.execute('''SELECT id,title,category,preparation,ingredients_json,
                             nutrition_status FROM zr_recipes WHERE active=1 ORDER BY id''')
    report=[]
    for rid,title,category,preparation,ingredients,status in rows:
        flags=list(quality_flags(title,category,preparation,ingredients))
        if status!='verified':flags.append('nutrition_unverified')
        report.append({'recipe_id':rid,'title':title,'quality_flags':tuple(flags)})
    return tuple(report)
