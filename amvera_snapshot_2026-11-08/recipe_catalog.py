"""Independent, read-only-by-default recipe catalog for Жорик.

SQLite file: /data/jorik_recipe_catalog.sqlite3 on Amvera. Never opens the
existing prices, menus, users, or shopping-list databases.
Nutrition is either explicitly estimated or verified; unknown is SQL NULL.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from itertools import islice
from pathlib import Path

DEFAULT_PATH = '/data/jorik_recipe_catalog.sqlite3'
DIETS = {'обычное', 'пп', 'без глютена'}
MEALS = {'завтрак', 'второй завтрак', 'обед', 'полдник', 'ужин', 'перекус', 'десерт'}
COURSES = {'основное', 'салат', 'закуска', 'суп', 'гарнир', 'десерт', 'напиток', 'выпечка'}
NUTRITION_STATUSES = {'не рассчитано', 'оценка', 'проверено'}
GLUTEN_STATUSES = {'неизвестно', 'содержит', 'без глютеновых ингредиентов', 'проверено без глютена'}
ALLERGEN_STATUSES = {'не проверено', 'проверено'}
REVIEW_STATUSES = {'черновик', 'проверено', 'отклонено'}


@contextmanager
def _connection(path=None):
    db_path = Path(path or os.getenv('JORIK_RECIPE_CATALOG_DB', DEFAULT_PATH))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()


@contextmanager
def _read_connection(path=None):
    """Read-only access cannot create or mutate an absent catalog file."""
    db_path = Path(path or os.getenv('JORIK_RECIPE_CATALOG_DB', DEFAULT_PATH)).resolve()
    db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def initialize(path=None):
    """Create tables only, without modifying legacy Жорик tables."""
    with _connection(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS recipe_catalog (
              id INTEGER PRIMARY KEY,
              slug TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              course TEXT NOT NULL DEFAULT 'основное',
              description TEXT NOT NULL DEFAULT '',
              servings INTEGER NOT NULL CHECK(servings > 0),
              yield_g REAL CHECK(yield_g > 0),
              prep_minutes INTEGER CHECK(prep_minutes >= 0),
              cook_minutes INTEGER CHECK(cook_minutes >= 0),
              kcal_per_serving REAL CHECK(kcal_per_serving >= 0),
              protein_g REAL CHECK(protein_g >= 0),
              fat_g REAL CHECK(fat_g >= 0),
              carbs_g REAL CHECK(carbs_g >= 0),
              nutrition_status TEXT NOT NULL DEFAULT 'не рассчитано',
              nutrition_note TEXT NOT NULL DEFAULT '',
              gluten_status TEXT NOT NULL DEFAULT 'неизвестно',
              allergen_status TEXT NOT NULL DEFAULT 'не проверено',
              source_name TEXT NOT NULL DEFAULT '',
              source_reference TEXT NOT NULL DEFAULT '',
              review_status TEXT NOT NULL DEFAULT 'черновик',
              instructions_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS recipe_ingredients (
              recipe_id INTEGER NOT NULL REFERENCES recipe_catalog(id) ON DELETE CASCADE,
              position INTEGER NOT NULL,
              name TEXT NOT NULL,
              amount REAL NOT NULL CHECK(amount > 0),
              unit TEXT NOT NULL,
              optional INTEGER NOT NULL DEFAULT 0 CHECK(optional IN (0,1)),
              note TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(recipe_id, position)
            );
            CREATE TABLE IF NOT EXISTS recipe_cuisines (
              recipe_id INTEGER NOT NULL REFERENCES recipe_catalog(id) ON DELETE CASCADE,
              cuisine TEXT NOT NULL,
              PRIMARY KEY(recipe_id, cuisine)
            );
            CREATE TABLE IF NOT EXISTS recipe_diets (
              recipe_id INTEGER NOT NULL REFERENCES recipe_catalog(id) ON DELETE CASCADE,
              diet TEXT NOT NULL,
              PRIMARY KEY(recipe_id, diet)
            );
            CREATE TABLE IF NOT EXISTS recipe_meals (
              recipe_id INTEGER NOT NULL REFERENCES recipe_catalog(id) ON DELETE CASCADE,
              meal TEXT NOT NULL,
              PRIMARY KEY(recipe_id, meal)
            );
            CREATE TABLE IF NOT EXISTS recipe_allergens (
              recipe_id INTEGER NOT NULL REFERENCES recipe_catalog(id) ON DELETE CASCADE,
              allergen TEXT NOT NULL,
              PRIMARY KEY(recipe_id, allergen)
            );
            CREATE INDEX IF NOT EXISTS recipe_catalog_review ON recipe_catalog(review_status);
            CREATE INDEX IF NOT EXISTS recipe_cuisines_lookup ON recipe_cuisines(cuisine);
            CREATE INDEX IF NOT EXISTS recipe_diets_lookup ON recipe_diets(diet);
            CREATE INDEX IF NOT EXISTS recipe_meals_lookup ON recipe_meals(meal);
        ''')
        # Compatible with catalogs created by RECIPES_01 REV1/REV2.
        columns = {row['name'] for row in db.execute('PRAGMA table_info(recipe_catalog)')}
        if 'allergen_status' not in columns:
            db.execute("ALTER TABLE recipe_catalog ADD COLUMN allergen_status TEXT NOT NULL DEFAULT 'не проверено'")


def add_recipe(recipe, *, path=None):
    """Explicit curator import: no implicit writes from Qwen output."""
    slug = recipe['slug']
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,79}', slug):
        raise ValueError('Недопустимый код рецепта')
    if not isinstance(recipe.get('title'), str) or not recipe['title'].strip():
        raise ValueError('Нет названия рецепта')
    if len(recipe['title']) > 200:
        raise ValueError('Слишком длинное название рецепта')
    if recipe.get('course', 'основное') not in COURSES:
        raise ValueError('Недопустимый тип блюда')
    servings = recipe['servings']
    if type(servings) is not int or not 1 <= servings <= 100:
        raise ValueError('Количество порций вне диапазона')
    for field in ('yield_g', 'prep_minutes', 'cook_minutes'):
        value = recipe.get(field)
        if value is None:
            continue
        if type(value) not in (int, float) or not 0 <= value <= 100000:
            raise ValueError('Некорректное значение ' + field)
        if field == 'yield_g' and value == 0:
            raise ValueError('Выход блюда должен быть положительным')
    for field in ('description', 'source_name', 'source_reference', 'review_status'):
        value = recipe.get(field, '')
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError('Некорректный текст ' + field)
    nutrition = recipe.get('nutrition', {})
    if not isinstance(nutrition, dict):
        raise ValueError('Неверный формат КБЖУ')
    status = nutrition.get('status', 'не рассчитано')
    if status not in NUTRITION_STATUSES:
        raise ValueError('Неизвестный статус КБЖУ')
    nutrient_keys = ('kcal', 'protein_g', 'fat_g', 'carbs_g')
    numbers = [nutrition.get(k) for k in nutrient_keys]
    if any(n is not None and (type(n) not in (float, int) or not 0 <= n <= 10000)
           for n in numbers):
        raise ValueError('Недопустимые КБЖУ')
    if status == 'не рассчитано' and any(n is not None for n in numbers):
        raise ValueError('Цифры КБЖУ без статуса')
    if status == 'проверено' and any(n is None for n in numbers):
        raise ValueError('Для проверенного КБЖУ нужны все показатели')
    gluten = recipe.get('gluten_status', 'неизвестно')
    if gluten not in GLUTEN_STATUSES:
        raise ValueError('Неизвестный статус глютена')
    source_reference = recipe.get('source_reference', '')
    has_source = isinstance(source_reference, str) and bool(source_reference.strip())
    if (status == 'проверено' or gluten == 'проверено без глютена') and not has_source:
        raise ValueError('Для подтверждённого статуса требуется источник проверки')
    allergen_status = recipe.get('allergen_status', 'не проверено')
    if allergen_status not in ALLERGEN_STATUSES:
        raise ValueError('Недопустимый статус проверки аллергенов')
    if allergen_status == 'проверено' and not has_source:
        raise ValueError('Нельзя подтверждать аллергены без источника проверки')
    review_status = recipe.get('review_status', 'черновик')
    if review_status not in REVIEW_STATUSES:
        raise ValueError('Некорректный статус проверки рецепта')
    if (status == 'проверено' or gluten == 'проверено без глютена' or
            allergen_status == 'проверено') and review_status != 'проверено':
        raise ValueError('Подтверждённые данные требуют проверки рецепта')
    diets = set(recipe.get('diets', ['обычное']))
    meals = set(recipe.get('meals', []))
    if not diets <= DIETS or not meals <= MEALS:
        raise ValueError('Неизвестный тип питания или приём пищи')
    if 'без глютена' in diets and gluten not in ('без глютеновых ингредиентов', 'проверено без глютена'):
        raise ValueError('Нельзя маркировать как безглютеновое без проверки состава')
    ingredients = recipe.get('ingredients', [])
    if not ingredients or len(ingredients) > 100:
        raise ValueError('Нужны ингредиенты')
    for ing in ingredients:
        if not isinstance(ing, dict):
            raise ValueError('Неверный формат ингредиента')
        if not isinstance(ing.get('name'), str) or not ing['name'].strip() or len(ing['name']) > 200:
            raise ValueError('Ингредиент без названия')
        if type(ing.get('amount')) not in (float, int) or not 0 < ing['amount'] <= 100000:
            raise ValueError('Недопустимая масса ингредиента')
        if ing.get('unit') not in ('г', 'мл', 'шт.'):
            raise ValueError('Единица ингредиента должна быть г, мл или шт.')
        if not isinstance(ing.get('note', ''), str) or len(ing.get('note', '')) > 500:
            raise ValueError('Некорректная заметка ингредиента')
    for field in ('cuisines', 'allergens'):
        if not all(isinstance(x, str) and x.strip() for x in recipe.get(field, [])):
            raise ValueError('Недопустимое значение ' + field)
    instructions = recipe.get('instructions', [])
    if (not isinstance(instructions, list) or len(instructions) > 30 or
            not all(isinstance(x, str) and len(x) <= 500 for x in instructions)):
        raise ValueError('Неверные шаги рецепта')
    initialize(path)
    with _connection(path) as db:
        row = db.execute('''INSERT INTO recipe_catalog
            (slug,title,course,description,servings,yield_g,prep_minutes,cook_minutes,
             kcal_per_serving,protein_g,fat_g,carbs_g,nutrition_status,nutrition_note,
             gluten_status,allergen_status,source_name,source_reference,review_status,instructions_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (slug, recipe['title'].strip(), recipe.get('course', 'основное'), recipe.get('description', ''), servings,
             recipe.get('yield_g'), recipe.get('prep_minutes'), recipe.get('cook_minutes'),
             *numbers, status, nutrition.get('note', ''), gluten, allergen_status,
             recipe.get('source_name', ''), recipe.get('source_reference', ''),
             recipe.get('review_status', 'черновик'), json.dumps(instructions, ensure_ascii=False)) )
        recipe_id = row.lastrowid
        db.executemany('''INSERT INTO recipe_ingredients
            (recipe_id,position,name,amount,unit,optional,note) VALUES (?,?,?,?,?,?,?)''',
            [(recipe_id, i, x['name'].strip(), x['amount'], x['unit'],
              int(bool(x.get('optional'))), x.get('note', '')) for i, x in enumerate(ingredients, 1)])
        for table, values, col in (
            ('recipe_cuisines', recipe.get('cuisines', []), 'cuisine'),
            ('recipe_diets', diets, 'diet'),
            ('recipe_meals', meals, 'meal'),
            ('recipe_allergens', recipe.get('allergens', []), 'allergen')):
            db.executemany(f'INSERT INTO {table} (recipe_id,{col}) VALUES (?,?)',
                           [(recipe_id, value.strip()) for value in set(values)])
    return recipe_id


def find_recipes(*, cuisines=(), diets=(), meals=(), courses=(), exclude_allergens=(),
                 exploratory_allergen_filter=False, limit=30, path=None):
    """Unknown allergen completeness is excluded by default (fail closed).

    exploratory_allergen_filter=True returns unverified candidate recipes for
    human review, never a guarantee of allergen safety.
    """
    limit = max(1, min(int(limit), 100))
    clauses, params = [], []
    if courses:
        clauses.append('r.course IN (' + ','.join('?' for _ in courses) + ')')
        params.extend(courses)
    for table, col, values in (('recipe_cuisines', 'cuisine', cuisines),
                                ('recipe_diets', 'diet', diets),
                                ('recipe_meals', 'meal', meals)):
        if values:
            clauses.append('EXISTS (SELECT 1 FROM ' + table +
                           ' t WHERE t.recipe_id=r.id AND t.' + col +
                           ' IN (' + ','.join('?' for _ in values) + '))')
            params.extend(values)
    if exclude_allergens:
        if not exploratory_allergen_filter:
            clauses.append("r.allergen_status='проверено'")
        clauses.append('NOT EXISTS (SELECT 1 FROM recipe_allergens a WHERE a.recipe_id=r.id '
                       'AND a.allergen IN (' + ','.join('?' for _ in exclude_allergens) + '))')
        params.extend(exclude_allergens)
    sql = 'SELECT r.id,r.slug,r.title,r.course,r.servings,r.kcal_per_serving,r.nutrition_status,r.gluten_status,r.allergen_status FROM recipe_catalog r'
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY r.title LIMIT ?'
    with _read_connection(path) as db:
        return [dict(x) for x in db.execute(sql, (*params, limit))]


def get_recipe(recipe_id, *, path=None):
    with _read_connection(path) as db:
        row = db.execute('SELECT * FROM recipe_catalog WHERE id=?', (recipe_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['instructions'] = json.loads(result.pop('instructions_json'))
        for table, attr in [('recipe_cuisines', 'cuisine'), ('recipe_diets', 'diet'),
                            ('recipe_meals', 'meal'), ('recipe_allergens', 'allergen')]:
            result[table.removeprefix('recipe_')] = [x[0] for x in db.execute(
                f'SELECT {attr} FROM {table} WHERE recipe_id=? ORDER BY {attr}', (recipe_id,))]
        result['ingredients'] = [dict(x) for x in db.execute(
            'SELECT name,amount,unit,optional,note FROM recipe_ingredients WHERE recipe_id=? ORDER BY position',
            (recipe_id,))]
        return result


def for_qwen(recipe_ids, *, people=None, path=None):
    """Bounded factual context; no private user data, prices, or system prompt."""
    ids = list(islice(dict.fromkeys(int(x) for x in recipe_ids), 12))
    result = []
    for recipe_id in ids:
        row = get_recipe(recipe_id, path=path)
        if row is None:
            continue
        factor = (people / row['servings']) if type(people) is int and 1 <= people <= 30 else 1
        # A single very detailed recipe must not exhaust an LLM context window.
        ingredients = row['ingredients'][:25]
        instructions = row['instructions'][:8]
        result.append({
            'id': row['id'], 'title': row['title'], 'course': row['course'],
            'servings': people if factor != 1 else row['servings'],
            'cuisines': row['cuisines'], 'diets': row['diets'], 'meals': row['meals'],
            'gluten_status': row['gluten_status'], 'allergens': row['allergens'],
            'allergen_status': row['allergen_status'],
            'nutrition_status': row['nutrition_status'],
            'kcal_per_serving': row['kcal_per_serving'],
            'protein_g_per_serving': row['protein_g'],
            'fat_g_per_serving': row['fat_g'],
            'carbs_g_per_serving': row['carbs_g'],
            'ingredients_complete': len(ingredients) == len(row['ingredients']),
            'instructions_complete': len(instructions) == len(row['instructions']),
            'ingredients': [{'name': x['name'][:100], 'amount': round(x['amount'] * factor, 2),
                             'unit': x['unit']} for x in ingredients],
            'instructions': [step[:200] for step in instructions],
        })
    return result
