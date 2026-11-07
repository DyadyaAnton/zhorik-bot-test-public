"""Five logical catalog summaries over the existing legacy SQLite schema.

No migration, view creation, cross-user records or implicit materialization.
"""
from __future__ import annotations


def summary(db, owner_id):
    if type(owner_id) is not int or owner_id <= 0:
        raise ValueError('owner_id')
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def total(table, condition='', args=()):
        if table not in tables:
            return 0
        # All table names and WHERE clauses are fixed by source code.
        return int(db.execute(f'SELECT COUNT(*) FROM {table} {condition}', args).fetchone()[0])

    return {
        'products': total('pb_products'),
        'retailer_skus': total('pb_skus'),
        'price_observations': total('pb_observations'),
        'owner_prices': total('user_prices', 'WHERE user_id=?', (owner_id,)),
        'recipes': total('zr_recipes', 'WHERE active=1'),
        'menus': total('menus', 'WHERE owner_id=?', (owner_id,)),
        'nutrition_rows': total('nutrition_products'),
        'nutrition_reference': total('nutrition_products', "WHERE confidence='reference_book'"),
        'nutrition_candidates': total('nutrition_products', "WHERE confidence='candidate_unverified'"),
        'owner_lists': total('lists', 'WHERE owner_id=?', (owner_id,)),
        'missing_tables': sorted({'pb_products', 'pb_skus', 'pb_observations',
                                  'user_prices', 'zr_recipes', 'menus',
                                  'nutrition_products', 'lists'} - tables),
    }
