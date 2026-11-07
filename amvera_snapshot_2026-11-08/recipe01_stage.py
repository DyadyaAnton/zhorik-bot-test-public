"""Read-only reconciliation of source nutrition rows against canonical products.

The result is a review queue, NOT a verified nutrition catalog.  It does not
write SQLite or imply that a product is safe for menu generation.
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path


def canonical_key(value: str) -> str:
    return ' '.join(value.strip().casefold().replace('ё', 'е').split())


def build_review_queue(source_csv, products):
    """Map exact normalized names only; ambiguous names remain unresolved.

    products: iterable of (stable_id, canonical_name). Returned rows preserve
    the reference source page and every input nutrient value.
    """
    catalog = {}
    for product_id, name in products:
        catalog.setdefault(canonical_key(name), set()).add(str(product_id))
    with Path(source_csv).open(encoding='utf-8-sig', newline='') as handle:
        source = list(csv.DictReader(handle))
    required = {'source_file','source_page','printed_name','protein_g_100g',
                'fat_g_100g','carbohydrate_g_100g','kcal_100g','review_status'}
    if source and not required.issubset(source[0]):
        raise ValueError('Incomplete source CSV columns')
    rows = []
    seen = set()
    for entry in source:
        if entry['review_status'] != 'candidate_unverified':
            raise ValueError('Unexpected source review status')
        identity = (entry['source_file'],entry['source_page'],entry['printed_name'],
                    *(entry[name] for name in ('protein_g_100g','fat_g_100g',
                     'carbohydrate_g_100g','kcal_100g')))
        if identity in seen:
            continue
        seen.add(identity)
        matches = catalog.get(canonical_key(entry['printed_name']), set())
        status = 'exact_name_candidate' if len(matches)==1 else (
                 'ambiguous_name' if matches else 'unmatched_name')
        rows.append({**entry, 'matched_product_id':next(iter(matches)) if len(matches)==1 else '',
                     'match_status':status, 'promotion_status':'needs_manual_review'})
    return rows


def summary(rows):
    counts = Counter(row['match_status'] for row in rows)
    return {name:counts[name] for name in
            ('exact_name_candidate','ambiguous_name','unmatched_name')}


def write_review_csv(rows, destination):
    if not rows:
        raise ValueError('No rows to write')
    with Path(destination).open('w',encoding='utf-8-sig',newline='') as fp:
        writer=csv.DictWriter(fp,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
