"""Conservative bridge for owner-reviewed PRICE07 discoveries into menu planning.

Only gram-measured products with audited complete nutrition may enter a menu.
With any declared allergy we intentionally exclude newly discovered products
until a separately tested allergen synonym/ingredient crosswalk exists.
"""
import json
import math

from price07_rev2_core import eligible_for_menu


def reviewed_menu_products(db, existing_names=()):
    names = {' '.join(str(n).casefold().replace('ё', 'е').split()) for n in existing_names}
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('p7_discovered_products','p7_nutrition_review','p7_nutrition_review_audit')")}
    if len(tables) != 3:
        return []
    output = []
    for code, name, kcal, allergens in db.execute('''
        SELECT d.code,d.canonical_name,n.kcal_100g,n.allergens_json
        FROM p7_discovered_products d JOIN p7_nutrition_review n
        ON n.product_code=d.code ORDER BY d.code'''):
        key = ' '.join(name.casefold().replace('ё', 'е').split())
        if not key or key in names or not eligible_for_menu(db, code):
            continue
        try:
            listed = json.loads(allergens)
            if not isinstance(listed, list) or any(not isinstance(v, str) for v in listed):
                continue
            value = float(kcal)
            if not math.isfinite(value) or not 0 <= value <= 900:
                continue
        except (ValueError, TypeError):
            continue
        names.add(key)
        output.append({'name': name, 'unit': 'г', 'price07_code': code,
                       'price07_kcal_100g': value,
                       'price07_allergens': listed})
    return output


def menu_energy(name, grams, catalog):
    """Prefer audited nutrition for discovered products; no synthetic portion."""
    from calorie_catalog import energy
    item = catalog.get(' '.join(str(name).casefold().replace('ё', 'е').split()))
    if item and 'price07_code' in item:
        if type(grams) not in (int, float) or not math.isfinite(grams) or grams <= 0:
            return None
        return round(item['price07_kcal_100g'] * grams / 100, 1)
    return energy(name, grams)


def menu_blocked(name, prefs, catalog, legacy_blocked):
    if legacy_blocked(name, prefs):
        return True
    item = catalog.get(' '.join(str(name).casefold().replace('ё', 'е').split()))
    # Conservative fail-closed: a new SKU's allergen metadata is not yet
    # certified to match all of the legacy user-preference synonyms.
    return bool(item and 'price07_code' in item and prefs.get('allergy'))
