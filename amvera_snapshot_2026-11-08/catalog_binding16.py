"""Offline-first manual ingredient bindings. NEVER called on startup/live by 16B.

Call init_schema only on a copied SQLite backup until migrations are accepted.
Binding does not verify KBJU, allergens, package size, store offer or price.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS zr_ingredient_bindings16 (
 recipe_id TEXT NOT NULL REFERENCES zr_recipes(id),
 ingredient_index INTEGER NOT NULL CHECK(ingredient_index >= 0),
 source_hash TEXT NOT NULL CHECK(length(source_hash) = 64),
 product_code TEXT NOT NULL REFERENCES pb_products(code),
 reviewed_by INTEGER NOT NULL CHECK(reviewed_by > 0),
 reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(recipe_id, ingredient_index)
);
CREATE TABLE IF NOT EXISTS zr_ingredient_binding_events16 (
 id INTEGER PRIMARY KEY,
 recipe_id TEXT NOT NULL,
 ingredient_index INTEGER NOT NULL,
 source_hash TEXT NOT NULL,
 product_code TEXT NOT NULL,
 reviewed_by INTEGER NOT NULL,
 reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def init_schema_on_copy(db: sqlite3.Connection, *, verified_backup=False):
    """Requires an explicit backup-confirmed invocation, never auto-called."""
    if verified_backup is not True:
        raise PermissionError('Сначала подготовить и проверить отдельную копию SQLite')
    needed = {'zr_recipes', 'pb_products'}
    found = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not needed <= found:
        raise ValueError('Нет обязательных родительских таблиц')
    db.executescript(SCHEMA)


def ingredient_hash(ingredient):
    if not isinstance(ingredient, dict) or not isinstance(ingredient.get('product'), str):
        raise ValueError('Некорректный ингредиент')
    raw = json.dumps(ingredient, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _source_hash(db, recipe_id, index):
    row = db.execute('SELECT ingredients_json FROM zr_recipes WHERE id=?', (recipe_id,)).fetchone()
    if row is None:
        raise ValueError('Рецепт не найден')
    try:
        ingredients = json.loads(row[0])
    except (ValueError, TypeError) as exc:
        raise ValueError('Повреждённый рецепт') from exc
    if not isinstance(ingredients, list) or not 0 <= index < len(ingredients):
        raise ValueError('Неверный индекс ингредиента')
    return ingredient_hash(ingredients[index])


def manually_bind_on_copy(db, *, recipe_id, ingredient_index, product_code,
                          reviewed_by, expected_hash):
    """Explicit owner review only. Never attempts inferred/automatic linking."""
    if (type(ingredient_index) is not int or ingredient_index < 0 or
            type(reviewed_by) is not int or reviewed_by <= 0):
        raise ValueError('Некорректные индекс или ID проверяющего')
    if not isinstance(product_code, str) or not product_code.strip():
        raise ValueError('Код продукта обязателен')
    actual = _source_hash(db, recipe_id, ingredient_index)
    if expected_hash != actual:
        raise ValueError('Ингредиент изменён после предварительного просмотра')
    if not db.execute('SELECT 1 FROM pb_products WHERE code=?', (product_code,)).fetchone():
        raise ValueError('Код продукта не найден в каноническом каталоге')
    # Atomic even when the caller uses SQLite autocommit (isolation_level=None).
    db.execute('SAVEPOINT manual_ingredient_binding16')
    try:
        db.execute('''INSERT INTO zr_ingredient_bindings16
                      (recipe_id,ingredient_index,source_hash,product_code,reviewed_by)
                      VALUES (?,?,?,?,?)
                      ON CONFLICT(recipe_id,ingredient_index) DO UPDATE SET
                        source_hash=excluded.source_hash, product_code=excluded.product_code,
                        reviewed_by=excluded.reviewed_by, reviewed_at=CURRENT_TIMESTAMP''',
                   (recipe_id, ingredient_index, actual, product_code, reviewed_by))
        db.execute('''INSERT INTO zr_ingredient_binding_events16
                      (recipe_id,ingredient_index,source_hash,product_code,reviewed_by)
                      VALUES(?,?,?,?,?)''', (recipe_id, ingredient_index, actual, product_code, reviewed_by))
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT manual_ingredient_binding16')
        raise
    finally:
        db.execute('RELEASE SAVEPOINT manual_ingredient_binding16')


def active_binding(db, recipe_id, ingredient_index):
    """Fail closed when ingredients were edited or reordered after review."""
    row = db.execute('''SELECT source_hash,product_code FROM zr_ingredient_bindings16
                        WHERE recipe_id=? AND ingredient_index=?''',
                     (recipe_id, ingredient_index)).fetchone()
    if row is None:
        return None
    try:
        return row[1] if row[0] == _source_hash(db, recipe_id, ingredient_index) else None
    except ValueError:
        return None
