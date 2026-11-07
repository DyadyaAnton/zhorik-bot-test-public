"""RECIPE-01 pure nutrition calculations and staging review helpers.

No database writes or implicit promotion of reference-book candidates.
Existing PRICE07 owner-review workflow remains the only review authority for
discovered products; this module does not bypass it.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Mapping, Sequence


class IncompleteNutrition(ValueError):
    """A complete meal macro claim is impossible with available evidence."""


@dataclass(frozen=True)
class Nutrients:
    kcal: float
    protein: float
    fat: float
    carbs: float

    def __post_init__(self):
        values = (self.kcal, self.protein, self.fat, self.carbs)
        if any(not isinstance(v, (int, float)) or isinstance(v, bool) or
               not isfinite(v) or v < 0 for v in values):
            raise ValueError('Nutrition values must be finite and nonnegative')

    def times(self, multiplier: float) -> 'Nutrients':
        if not isfinite(multiplier) or multiplier < 0:
            raise ValueError('Invalid ingredient multiplier')
        return Nutrients(*(v * multiplier for v in
                           (self.kcal, self.protein, self.fat, self.carbs)))

    def plus(self, other: 'Nutrients') -> 'Nutrients':
        return Nutrients(self.kcal + other.kcal, self.protein + other.protein,
                         self.fat + other.fat, self.carbs + other.carbs)


def calculate_recipe(ingredients: Sequence[Mapping], nutrition_by_product: Mapping,
                     *, servings: int, cooked_yield_g: float) -> dict:
    """Calculate dish, portion and cooked-100g totals.

    Each ingredient: {product_id, grams, state}. Nutrient catalog key is
    (product_id, state), with per-100g values, status='verified' and full
    allergen review. No ingredient can be silently ignored.
    """
    if type(servings) is not int or servings < 1 or servings > 1000:
        raise ValueError('Positive integer servings required')
    if not isinstance(cooked_yield_g, (int, float)) or not isfinite(cooked_yield_g) or cooked_yield_g <= 0:
        raise ValueError('Positive cooked yield required')
    if not ingredients:
        raise ValueError('Recipe must have ingredients')
    total = Nutrients(0, 0, 0, 0)
    raw_weight = 0.0
    allergens = set()
    warnings = []
    for ingredient in ingredients:
        product_id = ingredient.get('product_id')
        state = ingredient.get('state')
        grams = ingredient.get('grams')
        if not isinstance(product_id, str) or not product_id.strip() or not isinstance(state, str) or not state.strip():
            raise IncompleteNutrition('Unresolved product identity or food state')
        if type(grams) not in (int, float) or not isfinite(grams) or grams <= 0:
            raise ValueError('Positive ingredient grams required')
        record = nutrition_by_product.get((product_id, state))
        if record is None or record.get('status') != 'verified' or record.get('allergens_verified') is not True:
            raise IncompleteNutrition(f'Unverified nutrition/allergens: {product_id} ({state})')
        # A caller must load these fields from an audited trusted adapter.
        # Merely supplying a `verified` flag is not sufficient evidence.
        if not (isinstance(record.get('source'), str) and record['source'].strip() and
                isinstance(record.get('review_id'), str) and record['review_id'].strip()):
            raise IncompleteNutrition('Missing source or audit review identity')
        try:
            per100 = Nutrients(*(record[name] for name in ('kcal','protein','fat','carbs')))
        except (KeyError, TypeError) as exc:
            raise IncompleteNutrition('Missing nutrient fields') from exc
        if per100.protein + per100.fat + per100.carbs > 100.5:
            raise ValueError('Macronutrients exceed 100g of product')
        expected_kcal = 4*per100.protein + 9*per100.fat + 4*per100.carbs
        if abs(per100.kcal - expected_kcal) > max(35, per100.kcal*.2):
            warnings.append(f'{product_id}: kcal differs from approximate macro energy')
        if not isinstance(record.get('allergens'), (set, frozenset, tuple, list)):
            raise IncompleteNutrition('Missing allergen review')
        allergens.update(record['allergens'])
        total = total.plus(per100.times(grams/100))
        raw_weight += grams
    return {'dish':total, 'portion':total.times(1/servings),
            'per_100g_cooked':total.times(100/cooked_yield_g),
            'portion_g':cooked_yield_g/servings,
            'raw_ingredient_g':raw_weight, 'cooked_yield_g':cooked_yield_g,
            'allergens':tuple(sorted(allergens)), 'warnings':tuple(warnings)}


MEAL_SHARES = {'Завтрак':.27, 'Обед':.38, 'Ужин':.35}
MEAL_TOLERANCE = .05


def assess_meal_balance(meal_kcal: Mapping[str, float], daily_target: float) -> dict:
    """SOFT shares of actual daily calories, separate from user target check."""
    if not isfinite(daily_target) or daily_target <= 0:
        raise ValueError('Positive calorie target required')
    result = {}
    complete = all(type(meal_kcal.get(meal)) in (int, float) and
                   isfinite(meal_kcal[meal]) and meal_kcal[meal] >= 0
                   for meal in MEAL_SHARES)
    actual_daily = sum(meal_kcal[meal] for meal in MEAL_SHARES) if complete else 0
    for meal, share in MEAL_SHARES.items():
        kcal = meal_kcal.get(meal)
        if type(kcal) not in (int, float) or not isfinite(kcal) or kcal < 0:
            result[meal] = {'status':'incomplete','target_kcal':daily_target*share}
            continue
        if not complete or actual_daily <= 0:
            result[meal] = {'status':'incomplete','target_kcal':daily_target*share}
            continue
        observed_share = kcal/actual_daily
        result[meal] = {'status':'within_range' if abs(observed_share-share) <= MEAL_TOLERANCE + 1e-12 else 'soft_deviation',
                        'target_share':share, 'actual_share':observed_share,
                        'target_kcal':daily_target*share, 'actual_kcal':kcal,
                        'actual_daily_kcal':actual_daily,
                        'delta_percentage_points':(observed_share-share)*100}
    return result
