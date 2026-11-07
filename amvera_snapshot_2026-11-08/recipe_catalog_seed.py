"""Original illustrative starter recipes, not copied from supplied PDFs.

Approximate nutrition needs independent recalculation before production use.
Running twice is idempotent: existing recipes are never overwritten.
"""
from __future__ import annotations

import sqlite3

from recipe_catalog import add_recipe, initialize, _connection


def _ingredient(name, amount, unit='г'):
    return {'name': name, 'amount': amount, 'unit': unit}


def _recipe(slug, title, cuisines, diets, meals, ingredients, *, servings=2,
            kcal=None, protein=None, fat=None, carbs=None, gluten='неизвестно',
            allergens=(), steps=(), prep=15, cook=15):
    courses = {'greek_salad': 'салат', 'syrniki': 'десерт',
               'hummus': 'закуска', 'miso_tofu': 'суп', 'ratatouille': 'гарнир'}
    return {'slug': slug, 'title': title, 'course': courses.get(slug, 'основное'),
            'cuisines': cuisines, 'diets': diets,
            'meals': meals, 'servings': servings, 'prep_minutes': prep,
            'cook_minutes': cook, 'gluten_status': gluten, 'allergens': list(allergens),
            'ingredients': [_ingredient(*x) for x in ingredients],
            'nutrition': {'status': 'оценка' if kcal is not None else 'не рассчитано',
                          'kcal': kcal, 'protein_g': protein, 'fat_g': fat,
                          'carbs_g': carbs,
                          'note': 'Приблизительная оценка на порцию; не лабораторные данные.'},
            'instructions': list(steps), 'source_name': 'Авторские примеры Жорика',
            'review_status': 'черновик'}


SEEDS = [
    _recipe('greek_salad', 'Греческий салат с фетой',
            ['греческая', 'средиземноморская', 'европейская'], ['обычное', 'пп'],
            ['обед', 'ужин'], [('помидоры', 240), ('огурцы', 200), ('фета', 80),
                              ('маслины', 50), ('оливковое масло', 20), ('красный лук', 50)],
            kcal=250, protein=8, fat=20, carbs=11,
            allergens=['молоко'], steps=['Нарезать овощи.', 'Добавить фету и маслины.',
            'Заправить маслом непосредственно перед подачей.'], cook=0),
    _recipe('shakshuka', 'Шакшука с томатами',
            ['израильская', 'еврейская', 'средиземноморская', 'ближневосточная'],
            ['обычное', 'пп', 'без глютена'], ['завтрак', 'обед'],
            [('яйца', 4, 'шт.'), ('помидоры', 350), ('болгарский перец', 140),
             ('лук', 80), ('оливковое масло', 10)],
            kcal=260, protein=16, fat=16, carbs=12,
            gluten='без глютеновых ингредиентов', allergens=['яйца'],
            steps=['Потушить овощи с маслом.', 'Сделать углубления и добавить яйца.',
                   'Готовить под крышкой до желаемой плотности белка.']),
    _recipe('buckwheat_mushroom', 'Гречка с шампиньонами',
            ['русская', 'восточноевропейская'], ['обычное', 'пп', 'без глютена'],
            ['обед', 'ужин'], [('гречка сухая', 140), ('шампиньоны', 240),
                              ('лук', 80), ('масло растительное', 15)],
            kcal=330, protein=12, fat=10, carbs=49,
            gluten='без глютеновых ингредиентов',
            steps=['Отварить гречку.', 'Обжарить грибы и лук.', 'Смешать и прогреть.']),
    _recipe('syrniki', 'Сырники без пшеничной муки',
            ['русская', 'восточноевропейская'], ['обычное', 'пп'],
            ['завтрак', 'полдник', 'десерт'],
            [('творог', 360), ('яйцо', 1, 'шт.'), ('рисовая мука', 35),
             ('сахар', 15), ('масло растительное', 10)],
            kcal=360, protein=29, fat=17, carbs=20,
            allergens=['молоко', 'яйца'],
            steps=['Смешать творог, яйцо, муку и сахар.', 'Сформировать сырники.',
                   'Готовить на среднем огне с небольшим количеством масла.']),
    _recipe('ratatouille', 'Рататуй из овощей',
            ['французская', 'европейская', 'средиземноморская'],
            ['обычное', 'пп', 'без глютена'], ['обед', 'ужин'],
            [('баклажан', 250), ('кабачок', 250), ('помидоры', 300),
             ('болгарский перец', 180), ('оливковое масло', 25)],
            kcal=180, protein=4, fat=13, carbs=15,
            gluten='без глютеновых ингредиентов',
            steps=['Нарезать овощи одинаковыми кусочками.',
                   'Тушить с маслом до мягкости.'], cook=35),
    _recipe('chicken_rice', 'Курица с рисом и брокколи',
            ['азиатская', 'европейская'], ['обычное', 'пп', 'без глютена'],
            ['обед', 'ужин'], [('куриное филе', 300), ('рис сухой', 130),
                              ('брокколи', 260), ('масло растительное', 15)],
            kcal=460, protein=41, fat=12, carbs=45,
            gluten='без глютеновых ингредиентов',
            steps=['Отварить рис.', 'Запечь или потушить курицу до готовности.',
                   'Брокколи приготовить на пару.']),
    _recipe('hummus', 'Хумус домашний',
            ['еврейская', 'израильская', 'средиземноморская', 'ближневосточная'],
            ['обычное', 'пп', 'без глютена'], ['перекус', 'обед'],
            [('нут варёный', 300), ('тахини', 40), ('лимонный сок', 30, 'мл'),
             ('оливковое масло', 15), ('чеснок', 10)],
            kcal=320, protein=12, fat=16, carbs=32,
            gluten='без глютеновых ингредиентов', allergens=['кунжут'],
            steps=['Измельчить нут с тахини и лимонным соком.',
                   'Добавить масло и воду до нужной консистенции.'], cook=0),
    _recipe('miso_tofu', 'Мисо-суп с тофу',
            ['японская', 'азиатская'], ['обычное', 'пп'], ['обед', 'ужин'],
            [('тофу', 180), ('мисо-паста', 40), ('водоросли вакаме', 8),
             ('зелёный лук', 30), ('вода', 700, 'мл')],
            kcal=150, protein=12, fat=7, carbs=10,
            gluten='неизвестно', allergens=['соя'],
            steps=['Замочить вакаме и нагреть воду.', 'Добавить тофу.',
                   'Развести мисо-пасту в небольшом количестве бульона и ввести без кипячения.']),
    _recipe('lobio', 'Лобио из красной фасоли',
            ['грузинская', 'кавказская'], ['обычное', 'пп', 'без глютена'],
            ['обед', 'ужин'], [('фасоль красная варёная', 400), ('лук', 100),
                              ('грецкие орехи', 40), ('кинза', 20),
                              ('масло растительное', 15)],
            kcal=390, protein=17, fat=19, carbs=41,
            gluten='без глютеновых ингредиентов', allergens=['орехи'],
            steps=['Потушить лук.', 'Смешать фасоль с орехами и зеленью.',
                   'Прогреть вместе, сохраняя часть фасоли целой.']),
]


def seed(*, path=None):
    """Return number added. Never modify an existing slug, including user edits."""
    initialize(path)
    inserted = 0
    for recipe in SEEDS:
        with _connection(path) as db:
            exists = db.execute('SELECT 1 FROM recipe_catalog WHERE slug=?',
                                (recipe['slug'],)).fetchone()
        if not exists:
            try:
                add_recipe(recipe, path=path)
                inserted += 1
            except sqlite3.IntegrityError:
                # A second worker may have inserted this slug after our SELECT.
                with _connection(path) as db:
                    if not db.execute('SELECT 1 FROM recipe_catalog WHERE slug=?',
                                      (recipe['slug'],)).fetchone():
                        raise
    return inserted


if __name__ == '__main__':
    print('Добавлено рецептов:', seed())
