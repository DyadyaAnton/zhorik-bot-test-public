"""Soft RECIPE-01 meal balance from the exact saved ingredient portions.

This is a diagnostic of *estimated energy*, never verified recipe KBJU.
It only reads the menu snapshot and does not contact external services.
"""
from __future__ import annotations

from calorie_catalog import energy
from recipe01_nutrition import assess_meal_balance


def balance_for_menu(menu, daily_target):
    result = {}
    for day, meals in menu['menu'].items():
        meal_kcal = {}
        for role in ('Завтрак', 'Обед', 'Ужин'):
            dishes = meals.get(role)
            if not dishes:
                continue
            subtotal = 0.0
            complete = True
            for dish in dishes:
                ingredients = dish.get('ingredients')
                if not ingredients:
                    complete = False
                    break
                for ingredient in ingredients:
                    if ingredient.get('unit') not in ('г', 'кг'):
                        complete = False
                        break
                    factor = 1000 if ingredient['unit'] == 'кг' else 1
                    kcal = energy(ingredient['product'], ingredient['amount'] * factor)
                    if kcal is None:
                        complete = False
                        break
                    subtotal += kcal
                if not complete:
                    break
            if complete:
                meal_kcal[role] = subtotal
        result[day] = assess_meal_balance(meal_kcal, daily_target)
    return result
