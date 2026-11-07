"""Read-only RECIPE-01 readiness inventory of the existing recipe catalog.

This is NOT an approval path. It never modifies data or treats a user-supplied
``verified`` string or a source-book estimate as audited nutrient evidence.
"""
from __future__ import annotations

import json


def catalog_readiness(db, *, pilot_target=100):
    """Show gaps before owner/editor audit, without minting verified recipes."""
    if type(pilot_target) is not int or pilot_target < 1:
        raise ValueError('Positive pilot target required')
    rows=db.execute('''SELECT id,title,ingredients_json,nutrition_status,active
                       FROM zr_recipes ORDER BY id''').fetchall()
    has_yields=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zr_recipe_yields'").fetchone() is not None
    yields={r[0]:(r[1],r[2]) for r in db.execute('SELECT recipe_id,cooked_grams,yield_status FROM zr_recipe_yields')} if has_yields else {}
    counts={'total':len(rows),'active':0,'estimated':0,'missing_ingredients':0,
            'missing_yield':0,'eligible_with_verified_kbju':0}
    candidates=[]
    for rid,title,ingredients_json,status,active in rows:
        if not active:continue
        counts['active']+=1
        if status!='verified':counts['estimated']+=1
        try:
            ingredients=json.loads(ingredients_json)
            has_ingredients=isinstance(ingredients,list) and bool(ingredients) and all(
                isinstance(i,dict) and i.get('product') and i.get('amount') and i.get('unit') for i in ingredients)
        except (ValueError,TypeError):
            has_ingredients=False
        if not has_ingredients:counts['missing_ingredients']+=1
        cooked,yield_status=yields.get(rid,(None,None))
        verified_yield=type(cooked) in (float,int) and cooked>0 and yield_status=='verified'
        if not verified_yield:counts['missing_yield']+=1
        reasons=[]
        if status!='verified':reasons.append('nutrition_unverified')
        if not has_ingredients:reasons.append('ingredients_incomplete')
        if not verified_yield:reasons.append('yield_unverified')
        # Even a verified legacy flag does not prove independent food evidence;
        # this inventory is intentionally not an admission API.
        reasons.append('ingredient_audit_not_connected')
        candidates.append({'recipe_id':rid,'title':title,'review_reasons':tuple(reasons)})
    counts['remaining_to_pilot']=pilot_target
    counts['pilot_target']=pilot_target
    counts['ready']=False
    return {'counts':counts,'candidates':tuple(candidates)}
