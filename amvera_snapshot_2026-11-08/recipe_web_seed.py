"""Small attributed catalogue of ingredient facts from Povar.ru (no prose copied).

Records remain review-required; nutrition as published per 100g is not treated
as independently validated, and no dietary/allergen safety label is inferred.
"""
from __future__ import annotations

import json

from recipe_catalog import _connection, _read_connection
from recipe_pdf_import import initialize_sources


RECIPES = [
    dict(title='Греческий салат классический', url='https://m.povar.ru/recipes/grecheskii_salat_klassicheskii-822.html',
         cuisines=['греческая', 'средиземноморская', 'европейская'], meals=['обед', 'ужин'], servings=3, minutes=15,
         kcal=None, ingredients=['огурец — 1 шт.', 'помидоры — 2–3 шт.', 'сладкий перец — 1/2 шт.',
         'красный лук — 1/2 шт.', 'мягкий сыр — 100 г', 'маслины — 50 г',
         'лимон — 1/2 шт.', 'оливковое масло — 2 ст. л.', 'орегано — 1/2 ч. л.', 'соль — щепотка']),
    dict(title='Сырники классические', url='https://m.povar.ru/recipes/syrniki_klassicheskie-51134.html',
         cuisines=['русская', 'европейская'], meals=['завтрак', 'полдник'], servings=3, minutes=30,
         kcal=None, ingredients=['творог — 500 г', 'яйцо — 1 шт.', 'сахар — 2–3 ст. л.',
         'пшеничная мука — 4 ст. л.', 'соль — щепотка', 'ванильный сахар — 0,5 ч. л.',
         'растительное масло — 100 мл']),
    dict(title='Мисо-суп классический', url='https://m.povar.ru/recipes/miso_sup_klassicheskii-59595.html',
         cuisines=['японская', 'азиатская'], meals=['обед', 'ужин'], servings=1, minutes=15,
         kcal=None, ingredients=['вода — 550 мл', 'шёлковый тофу — 150 г', 'вакаме — 1 ст. л.',
         'мисо-паста — 1 ст. л.', 'зелёный лук — 30 г', 'сухой рыбный бульон даси — 10 г']),
    dict(title='Лобио из красной фасоли', url='https://m.povar.ru/recipes/lobio_iz_fasoli-8252.html',
         cuisines=['грузинская', 'кавказская'], meals=['обед', 'ужин'], servings=6, minutes=660,
         kcal=None, ingredients=['сухая красная фасоль — 200 г', 'лук — 1 шт.', 'чеснок — 4 зубчика',
         'грецкие орехи — 70 г', 'кинза — 1 пучок', 'хмели-сунели — 1 ч. л.',
         'кавказские специи — 1 ч. л.', 'перец красный, чёрный, соль — по вкусу']),
    dict(title='Шакшука', url='https://m.povar.ru/recipes/shakshuka-1041.html',
         cuisines=['еврейская', 'израильская', 'ближневосточная'], meals=['завтрак'], servings=2, minutes=20,
         kcal=None, ingredients=['яйца — 3 шт.', 'томаты в собственном соку — 300 г',
         'лук — 70 г', 'сладкий перец — 80 г', 'чеснок — 2 зубчика',
         'петрушка — 1 пучок', 'оливковое масло — 2 ст. л.',
         'копчёная паприка — 1 ч. л.', 'чили — щепотка', 'соль, перец — по вкусу']),
]


def initialize_web(path=None):
    initialize_sources(path)
    with _connection(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS recipe_web_candidates (
            id INTEGER PRIMARY KEY,
            source_url TEXT UNIQUE NOT NULL,
            source_name TEXT NOT NULL,
            title TEXT NOT NULL,
            cuisines_json TEXT NOT NULL,
            meals_json TEXT NOT NULL,
            ingredients_json TEXT NOT NULL,
            servings INTEGER,
            minutes INTEGER,
            kcal_per_100g REAL,
            review_status TEXT NOT NULL DEFAULT 'требует проверки',
            instruction_url TEXT NOT NULL
        )''')


def seed_web(path=None):
    initialize_web(path)
    with _connection(path) as db:
        before = db.total_changes
        db.executemany('''INSERT OR IGNORE INTO recipe_web_candidates
          (source_url,source_name,title,cuisines_json,meals_json,ingredients_json,
           servings,minutes,kcal_per_100g,instruction_url)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
           [(r['url'], 'Povar.ru', r['title'], json.dumps(r['cuisines'], ensure_ascii=False),
             json.dumps(r['meals'], ensure_ascii=False), json.dumps(r['ingredients'], ensure_ascii=False),
             r['servings'], r['minutes'], r['kcal'], r['url']) for r in RECIPES])
        return db.total_changes - before


def find_web(*, path=None, limit=50):
    with _read_connection(path) as db:
        return [dict(r) for r in db.execute('''SELECT source_url,title,cuisines_json,meals_json,
             ingredients_json,servings,minutes,kcal_per_100g,review_status,instruction_url
             FROM recipe_web_candidates ORDER BY id LIMIT ?''', (max(1, min(int(limit), 100)),))]


if __name__ == '__main__':
    print('Добавлено кандидатов Povar.ru:', seed_web())
