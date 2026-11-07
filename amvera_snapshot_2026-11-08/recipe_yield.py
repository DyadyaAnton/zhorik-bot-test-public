"""REV7: indicative cooked yield. Never claim a measured cooked mass.

The input amounts are raw grams per recipe portion. Soup water is absent
from the recipe JSON; no mathematically sound cooked yield can be inferred.
"""

REFERENCE = {
    'гречка': 3.2, 'гречневая крупа': 3.2, 'рис': 3.0,
    'макароны': 3.0, 'перловая крупа': 2.95,
    'чечевица': 2.85, 'нут': 2.25, 'киноа': 3.15,
}
MEAT_STEMS = ('кури', 'индей', 'говя', 'свин', 'баран', 'треск', 'минтай', 'хек', 'рыб', 'скумбр', 'горбуш')


def estimate_yield(recipe):
    """Return raw grams, indicative cooked grams, evidence status, standard portion.

    Reference factors for grains are estimates, never measurements. Unknown
    factors (esp. meat/fish) downgrade status to orientational. Soups with
    unlisted water are unverified; the 250 g serving is a *role guideline*.
    """
    raw = 0.0
    cooked = 0.0
    status = 'reference_estimate'
    for item in recipe['ingredients']:
        if item['unit'] != 'г':
            return {'raw_grams': None, 'cooked_grams': None,
                    'yield_status': 'unverified', 'standard_portion_grams': None}
        qty = float(item['amount'])
        raw += qty
        key = item['product'].casefold().replace('ё', 'е')
        if key in REFERENCE:
            factor = REFERENCE[key]
        elif any(key.startswith(stem) for stem in MEAT_STEMS):
            factor = 0.78
            status = 'orientational'
        else:
            factor = 1.0
            status = 'orientational'
        cooked += qty * factor
    if recipe['category'] == 'Супы':
        return {'raw_grams': round(raw, 1), 'cooked_grams': None,
                'yield_status': 'unverified_no_water', 'standard_portion_grams': 250}
    return {'raw_grams': round(raw, 1), 'cooked_grams': round(cooked, 1),
            'yield_status': status, 'standard_portion_grams': None}
