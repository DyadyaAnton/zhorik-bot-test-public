"""Read-only catalog bridge preview (16B): never merges or edits live SQLite.

Shows only safe aggregate counts and schema relationships. A match is a proposal,
not permission to change an existing SKU, nutrition value, price or recipe.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import re
import sqlite3


def _key(value):
    if not isinstance(value, str):
        return ''
    return re.sub(r'\s+', ' ', value.strip().casefold().replace('ё', 'е'))


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


_FAMILY_STARTS = {
    'яйца': ('яйца ',), 'творог': ('творог ',), 'рис': ('рис ',),
    'мука': ('мука ',), 'гречка': ('гречневая крупа',),
    'говядина': ('говядина ',), 'лук': ('лук ',),
    'молоко': ('молоко ',), 'макароны': ('макароны ',),
    'свинина': ('свинина ',), 'масло сливочное': ('масло сливочное ',),
    'хлеб белый': ('хлеб пшеничный',),
    'орехи': ('грецкие орехи',),
}


def _family_candidates(unmatched_name, catalog_names):
    """Only candidate families, never automatic mappings or nutrition values."""
    prefixes = _FAMILY_STARTS.get(unmatched_name, ())
    return sorted(name for name in catalog_names if any(name.startswith(prefix) for prefix in prefixes))


def preview(db: sqlite3.Connection) -> dict:
    """Never includes user rows or raw recipe/price data in the report."""
    tables = _tables(db)
    required = {'pb_products', 'zb_canonical_products', 'zr_recipes'}
    result = {'mode': 'read_only', 'schema_ready': required <= tables,
              'counts': {}, 'links': {}, 'warnings': []}
    if not result['schema_ready']:
        result['warnings'].append('Недостающие таблицы: ' + ', '.join(sorted(required - tables)))
        return result
    pb = [tuple(row) for row in db.execute('SELECT code,name,base_unit FROM pb_products')]
    zb = [tuple(row) for row in db.execute('SELECT code,name,base_unit FROM zb_canonical_products')]
    recipes = [row[0] for row in db.execute('SELECT ingredients_json FROM zr_recipes')]
    result['counts'] = {'pb_products': len(pb), 'zb_canonical_products': len(zb), 'recipes': len(recipes)}
    maps = {}
    for name, items in (('pb', pb), ('zb', zb)):
        index = defaultdict(set)
        for code, title, unit in items:
            index[_key(title)].add((str(code), str(unit)))
        maps[name] = index
    pb_idx, zb_idx = maps['pb'], maps['zb']
    common = pb_idx.keys() & zb_idx.keys()
    exact = 0
    unit_conflicts = 0
    ambiguous = 0
    for name in common:
        a, b = pb_idx[name], zb_idx[name]
        if len(a) != 1 or len(b) != 1:
            ambiguous += 1
        elif next(iter(a))[1] != next(iter(b))[1]:
            unit_conflicts += 1
        else:
            exact += 1
    result['links']['pb_zb_exact_candidates'] = exact
    result['links']['pb_zb_unit_conflicts'] = unit_conflicts
    result['links']['pb_zb_ambiguous'] = ambiguous
    result['links']['pb_without_zb_name'] = len(pb_idx.keys() - zb_idx.keys())
    result['links']['zb_without_pb_name'] = len(zb_idx.keys() - pb_idx.keys())
    ingredient_counts = Counter()
    unmatched_counts = Counter()
    for raw in recipes:
        try:
            ingredients = json.loads(raw)
        except (TypeError, ValueError):
            ingredient_counts['malformed_recipe_json'] += 1
            continue
        if not isinstance(ingredients, list):
            ingredient_counts['malformed_recipe_json'] += 1
            continue
        for ingredient in ingredients:
            if not isinstance(ingredient, dict):
                ingredient_counts['invalid_ingredient'] += 1
                continue
            name = _key(ingredient.get('product'))
            if not name:
                ingredient_counts['invalid_ingredient'] += 1
            elif len(pb_idx.get(name, ())) == 1:
                ingredient_counts['exact_product_name'] += 1
            elif pb_idx.get(name):
                ingredient_counts['ambiguous_name'] += 1
            else:
                ingredient_counts['unmatched_name'] += 1
                unmatched_counts[name] += 1
    result['links']['recipe_ingredients'] = dict(sorted(ingredient_counts.items()))
    candidates = {name: _family_candidates(name, pb_idx) for name in unmatched_counts}
    result['links']['unmatched_unique_names'] = len(unmatched_counts)
    result['links']['with_family_variants_occurrences'] = sum(
        n for name, n in unmatched_counts.items() if candidates[name])
    result['links']['without_family_variants_occurrences'] = sum(
        n for name, n in unmatched_counts.items() if not candidates[name])
    result['links']['with_family_variants_unique'] = sum(bool(v) for v in candidates.values())
    result['links']['without_family_variants_unique'] = sum(not v for v in candidates.values())
    result['warnings'].extend([
        'Только имена и базовые единицы: граммовки рецептов не приведены к SKU.',
        'Совпадения не утверждают пищевую ценность, цену, фасовку, регион или аллерген.',
        'Предложенные товарные семейства — только для ручной редакционной проверки.',
        'Никаких UPDATE, INSERT, DELETE, CREATE или сетевых запросов не выполнялось.',
    ])
    return result
