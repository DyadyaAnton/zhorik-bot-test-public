"""Conservative comparison of independent reference nutrition candidates.

Conflicts are REVIEW SIGNALS, not nutrition approvals. No uncertain number is
silently reduced to zero or to a mean. Each row must have the same explicit
measurement basis before comparable values can be reported.
"""
from __future__ import annotations

import math
import re


def numeric_cell(value):
    """Return an exact nonnegative scalar or None for ranges/dashes/ambiguity."""
    if type(value) in (int,float):
        return float(value) if math.isfinite(value) and value>=0 else None
    if not isinstance(value,str):return None
    v=value.strip().replace(',','.')
    if not re.fullmatch(r'\d+(?:\.\d+)?',v):return None
    result=float(v)
    return result if math.isfinite(result) else None


FIELDS=('protein','fat','carbs','kcal')
BASES={'per_100g_edible','per_100g_raw','per_100g_cooked'}
STATES={'raw','dry','cooked'}


def compare_candidates(left,right,*,tolerance_percent=10.0):
    """Match only identical declared measurement basis AND food state.

    Returns per-field differences; never returns verified nutritional values.
    """
    if type(tolerance_percent) not in (int,float) or not 0<=tolerance_percent<=100:
        raise ValueError('Invalid review tolerance')
    if (left.get('basis') not in BASES or
        left.get('basis')!=right.get('basis') or
        left.get('state') not in STATES or
        left.get('state')!=right.get('state')):
        return {'status':'not_comparable','details':{},'verified':False}
    details={}
    for field in FIELDS:
        x=numeric_cell(left.get(field));y=numeric_cell(right.get(field))
        if x is None or y is None:
            details[field]={'status':'missing_or_ambiguous'}
            continue
        denom=max(x,y)
        gap_percent=100*abs(x-y)/denom if denom else 0
        absolute_limit=15 if field=='kcal' else 2
        details[field]={'status':'disagreement' if gap_percent>tolerance_percent or abs(x-y)>absolute_limit else 'within_tolerance',
                        'difference_percent':round(gap_percent,2)}
    statuses={d['status'] for d in details.values()}
    overall=('disagreement' if 'disagreement' in statuses else
             'incomplete' if 'missing_or_ambiguous' in statuses else 'agreement_candidate')
    return {'status':overall,'details':details,'verified':False}
