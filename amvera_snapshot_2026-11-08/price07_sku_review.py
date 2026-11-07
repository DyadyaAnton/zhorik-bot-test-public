"""Explicit owner-reviewed SKU-to-canonical binding with durable audit.

Never verifies prices, region, nutrition, allergens, or menu eligibility.
No network operations are performed inside a write transaction.
"""
from __future__ import annotations

import json

from price07_sku_identity import assess, package_basis

AUDIT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS p7_sku_review_audit (
  id INTEGER PRIMARY KEY,
  sku_id INTEGER NOT NULL,
  retailer TEXT NOT NULL,
  external_id TEXT NOT NULL,
  previous_code TEXT,
  proposed_code TEXT NOT NULL,
  actor_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('manual_bind','manual_reject')),
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS p7_sku_review_audit_sku ON p7_sku_review_audit(sku_id,id);
'''


def init(db):
    """Additive schema only; production migration requires backup first."""
    if db.in_transaction:
        raise RuntimeError('SKU review schema migration cannot run inside an open transaction')
    db.executescript(AUDIT_SCHEMA)


def _validate_evidence(evidence, sku_title, code):
    if not isinstance(evidence, dict):
        raise ValueError('Structured human-review evidence required')
    if evidence.get('confirmed_title') != sku_title or evidence.get('confirmed_code') != code:
        raise ValueError('Explicit SKU title and code confirmation required')
    if any(evidence.get(k) is not True for k in ('brand_checked','package_checked','composition_checked')):
        raise ValueError('Brand, package and composition must be reviewed')
    source = evidence.get('reference')
    note = evidence.get('note')
    if not isinstance(source,str) or not 8 <= len(source.strip()) <= 500:
        raise ValueError('Evidence reference required')
    if not isinstance(note,str) or not 10 <= len(note.strip()) <= 500:
        raise ValueError('Review explanation required')
    return json.dumps(evidence,ensure_ascii=False,sort_keys=True)


def manual_bind(db, *, sku_id, canonical_code, actor_id, evidence):
    """Atomically bind one unreviewed SKU after explicit evidence and checks.

    Raises without changing the SKU/audit when a different binding already
    exists or attributes contradict the intended canonical product.
    """
    if type(sku_id) is not int or sku_id <= 0 or type(actor_id) is not int or actor_id <= 0:
        raise ValueError('Invalid identity')
    if not isinstance(canonical_code,str) or not canonical_code or len(canonical_code)>100:
        raise ValueError('Invalid canonical code')
    db.execute('SAVEPOINT p7_manual_bind')
    try:
        sku = db.execute('SELECT retailer,external_id,title,product_code,match_status FROM pb_skus WHERE id=?',
                         (sku_id,)).fetchone()
        canonical = db.execute('SELECT name,base_unit FROM pb_products WHERE code=?',(canonical_code,)).fetchone()
        if sku is None or canonical is None:
            raise ValueError('SKU or canonical product not found')
        retailer, external, title, old_code, old_status = sku
        if old_status != 'review_required' or old_code is not None:
            raise ValueError('SKU is already decided; cannot overwrite a previous decision')
        assessment = assess(title,canonical[0])
        if assessment['status'] != 'review_required':
            raise ValueError('SKU identity not proven by exact title: '+','.join(assessment['reasons']))
        observed_basis = package_basis(title)
        if observed_basis is None or observed_basis != canonical[1]:
            raise ValueError('SKU package unit missing or incompatible with canonical unit')
        serialized = _validate_evidence(evidence,title,canonical_code)
        cur = db.execute('''UPDATE pb_skus SET product_code=?,match_status='manually_verified',
            match_evidence=? WHERE id=? AND product_code IS NULL AND match_status='review_required' ''',
                         (canonical_code,serialized,sku_id))
        if cur.rowcount != 1:
            raise RuntimeError('Concurrent review update')
        db.execute('''INSERT INTO p7_sku_review_audit
            (sku_id,retailer,external_id,previous_code,proposed_code,actor_id,action,evidence_json)
            VALUES(?,?,?,?,?,?,'manual_bind',?)''',
                   (sku_id,retailer,external,old_code,canonical_code,actor_id,serialized))
        db.execute('RELEASE SAVEPOINT p7_manual_bind')
        return {'sku_id':sku_id,'canonical_code':canonical_code,'status':'manually_verified',
                'price_verified':False,'nutrition_verified':False}
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT p7_manual_bind')
        db.execute('RELEASE SAVEPOINT p7_manual_bind')
        raise
