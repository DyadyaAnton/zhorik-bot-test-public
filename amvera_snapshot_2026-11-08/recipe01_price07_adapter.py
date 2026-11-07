"""Read-only bridge from audited PRICE07 nutrition to RECIPE-01 calculations.

Only owner-reviewed discovered products are supported. This bridge cannot
promote legacy products or references without their own approved review flow.
"""
from __future__ import annotations

import json
from typing import Iterable

from price07_rev2_core import eligible_for_menu
from recipe01_nutrition import IncompleteNutrition


def load_audited_nutrition(db, product_codes: Iterable[str]) -> dict:
    """Return only current, audit-backed per-100g values pending basis review.

    The PRICE07 schema does not record a cooked/dry measurement basis; these
    values are therefore not usable as raw/cooked/dry entries. A separate
    evidence-backed basis review is required before allowing any such state.
    """
    codes = tuple(dict.fromkeys(product_codes))
    if any(not isinstance(code, str) or not code.startswith('NP') for code in codes):
        raise IncompleteNutrition('Only PRICE07 discovered product identities are supported')
    result = {}
    for code in codes:
        try:
            if not eligible_for_menu(db, code):
                raise IncompleteNutrition(f'No approved PRICE07 nutrition audit: {code}')
            row = db.execute('''SELECT r.kcal_100g,r.protein_100g,r.fat_100g,
                                      r.carbs_100g,r.allergens_json,r.source,r.actor_id,
                                      a.id,a.evidence_json
                FROM p7_nutrition_review r JOIN p7_nutrition_review_audit a
                  ON a.product_code=r.product_code AND a.actor_id=r.actor_id
                 AND a.event='owner_verified'
                WHERE r.product_code=? ORDER BY a.id DESC LIMIT 1''', (code,)).fetchone()
        except (AttributeError, ValueError) as exc:
            raise IncompleteNutrition('PRICE07 evidence unavailable') from exc
        if not row:
            raise IncompleteNutrition(f'Incomplete PRICE07 evidence: {code}')
        kcal, protein, fat, carbs, allergens_json, source, actor_id, review_id, evidence_json = row
        try:
            allergens = json.loads(allergens_json)
            evidence = json.loads(evidence_json)
            values = (kcal, protein, fat, carbs)
            expected = tuple(evidence[k] for k in ('kcal_100g','protein_100g','fat_100g','carbs_100g'))
            if (tuple(values) != expected or allergens != evidence['allergens'] or
                source != evidence['source'] or not isinstance(allergens, list) or
                not all(isinstance(a, str) for a in allergens) or
                type(actor_id) is not int or type(review_id) is not int):
                raise ValueError('Audit evidence differs from current nutrition')
        except (ValueError, TypeError, KeyError) as exc:
            raise IncompleteNutrition(f'PRICE07 nutrition audit mismatch: {code}') from exc
        result[(code, 'basis_unknown')] = {
            'kcal':kcal, 'protein':protein, 'fat':fat, 'carbs':carbs,
            'allergens':tuple(allergens), 'allergens_verified':True,
            'source':source, 'review_id':f'p7:{review_id}',
            'status':'needs_basis_review',
        }
    return result
