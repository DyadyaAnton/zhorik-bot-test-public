"""Review-only intake of external menu examples; never an approved recipe import.

The PDFs are rasterized web-page screenshots. Their printed macros and recipes
need independent transcription, ingredient-basis and editorial checks.
"""
from __future__ import annotations

import csv
from pathlib import Path

REQUIRED = ('source_document', 'source_day', 'printed_title', 'suggested_role',
            'review_status', 'nutrition_status')
ROLES = {'breakfast', 'lunch', 'dinner', 'component', 'unassigned'}


def read_source_candidates(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or any(key not in reader.fieldnames for key in REQUIRED):
            raise ValueError('Missing source candidate columns')
        rows = list(reader)
    if not rows:
        raise ValueError('Empty source candidate list')
    seen = set()
    for row in rows:
        if not row['source_document'].strip() or not row['printed_title'].strip():
            raise ValueError('Missing source identity')
        if row['source_day'] not in {'1', '2', '3', '4', '5'}:
            raise ValueError('Unknown source day')
        if row['suggested_role'] not in ROLES:
            raise ValueError('Unknown candidate role')
        if row['review_status'] != 'needs_editorial_review' or row['nutrition_status'] != 'unverified':
            raise ValueError('Source candidate cannot be pre-approved')
        identity = (row['source_document'], row['source_day'], row['printed_title'].casefold())
        if identity in seen:
            raise ValueError('Duplicate source title for a day')
        seen.add(identity)
    return tuple(rows)
