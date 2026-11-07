"""Conservative KBJU catalog seed for *copied* SQLite databases only.

Never silently upgrades candidate data to verified. A missing nutrient remains
NULL. Designed for owner-reviewed migration, not automatic bot startup.
"""
from __future__ import annotations

from price_b_catalog import seeds
from nutrition_reference import kbju_for
from nutrition_candidates import candidate_for


def planned_rows():
    result = []
    for code, title, category, unit in seeds():
        reference = kbju_for(title)
        candidate = candidate_for(title) if reference is None else None
        if reference is not None:
            values, status, source, page = reference, 'reference_book', 'Таблица калорийности продуктов (присланный PDF)', '1–2'
        elif candidate is not None:
            _, values, _ = candidate
            status, source, page = 'candidate_unverified', 'kbju_candidates_v1.csv', ''
        else:
            values, status, source, page = (None, None, None, None), 'missing', '', ''
        result.append((code, title, category, values, status, source, page))
    return result


def seed_on_copy(db, *, verified_backup=False):
    """Populate empty canonical product slots without overwriting past entries."""
    if verified_backup is not True:
        raise PermissionError('Подтвердите проверенную копию SQLite')
    required = {'nutrition_products', 'pb_products'}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required <= tables:
        raise ValueError('Требуемые таблицы отсутствуют')
    before = db.execute('SELECT count(*) FROM nutrition_products').fetchone()[0]
    db.execute('SAVEPOINT seed_nutrition16')
    try:
        # Stage unverified macros separately. No menu/nutrition calculation
        # reads this table; promotion is a future explicit manual operation.
        db.execute('''CREATE TABLE IF NOT EXISTS zl_nutrition_candidates16 (
                      product_code TEXT PRIMARY KEY REFERENCES pb_products(code),
                      canonical_name TEXT NOT NULL,
                      kcal_100g REAL, protein_100g REAL, fat_100g REAL, carbs_100g REAL,
                      source TEXT NOT NULL, review_status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(review_status IN ('pending','rejected','manually_approved'))
                      )''')
        for code, title, category, values, status, source, page in planned_rows():
            # Stable canonical name, no mutations to existing independently
            # reviewed user data. Product code relation is a separate stage.
            if not db.execute('SELECT 1 FROM nutrition_products WHERE canonical_name=? COLLATE NOCASE LIMIT 1',
                              (title,)).fetchone():
                safe_values = values if status == 'reference_book' else (None, None, None, None)
                db.execute('''INSERT OR IGNORE INTO nutrition_products
                              (canonical_name,preparation,category,kcal_100g,
                               protein_100g,fat_100g,carbs_100g,source,source_page,confidence)
                              VALUES(?, 'unspecified', ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (title, category, *safe_values, source, page, status))
            if status == 'candidate_unverified':
                db.execute('''INSERT OR IGNORE INTO zl_nutrition_candidates16
                              (product_code,canonical_name,kcal_100g,protein_100g,
                               fat_100g,carbs_100g,source) VALUES(?,?,?,?,?,?,?)''',
                           (code, title, *values, source))
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT seed_nutrition16')
        raise
    finally:
        db.execute('RELEASE SAVEPOINT seed_nutrition16')
    after = db.execute('SELECT count(*) FROM nutrition_products').fetchone()[0]
    pending = db.execute('SELECT COUNT(*) FROM zl_nutrition_candidates16 WHERE review_status=?', ('pending',)).fetchone()[0]
    return {'before': before, 'after': after, 'added': after - before, 'staged_candidates': pending,
            'planned_reference': sum(r[4] == 'reference_book' for r in planned_rows()),
            'planned_candidate': sum(r[4] == 'candidate_unverified' for r in planned_rows()),
            'planned_missing': sum(r[4] == 'missing' for r in planned_rows())}
