"""Explicit, backup-gated evidence for PRICE07 nutritional measurement basis.

No automatic promotion. This is a sidecar to the existing PRICE07 audit,
not a new product catalog or an independent source of nutrient numbers.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from urllib.parse import urlsplit

from recipe01_nutrition import IncompleteNutrition
from recipe01_price07_adapter import load_audited_nutrition

STATES = ('raw_edible','dry_edible','cooked_edible')

DDL = '''
CREATE TABLE IF NOT EXISTS r1_product_basis (
 product_code TEXT PRIMARY KEY REFERENCES p7_nutrition_review(product_code),
 state TEXT NOT NULL CHECK(state IN ('raw_edible','dry_edible','cooked_edible')),
 price07_audit_id INTEGER NOT NULL REFERENCES p7_nutrition_review_audit(id),
 source_url TEXT NOT NULL, note TEXT NOT NULL, actor_id INTEGER NOT NULL,
 evidence_hash TEXT NOT NULL, verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS r1_product_basis_audit (
 id INTEGER PRIMARY KEY, product_code TEXT NOT NULL, actor_id INTEGER NOT NULL,
 event TEXT NOT NULL CHECK(event='basis_owner_verified'),
 evidence_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS r1_basis_audit_code ON r1_product_basis_audit(product_code,id);
'''


def init_basis_schema(db:sqlite3.Connection, *, backup_verified=False):
    if not backup_verified or db.in_transaction:
        raise RuntimeError('Verified backup and idle DB required before RECIPE-01 migration')
    needed=('p7_nutrition_review','p7_nutrition_review_audit')
    present={r[0] for r in db.execute('SELECT name FROM sqlite_master WHERE type="table"')}
    if not set(needed)<=present:
        raise RuntimeError('PRICE07 nutrition audit must be initialized first')
    db.executescript(DDL)


def _sha(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,
                                     separators=(',',':')).encode('utf-8')).hexdigest()


def verify_product_basis(db, *, product_code, state, price07_audit_id,
                         source_url, note, actor_id):
    """One-time explicit owner basis attestation linked to real PRICE07 review."""
    if (state not in STATES or type(actor_id) is not int or actor_id<=0 or
        type(price07_audit_id) is not int or price07_audit_id<=0 or
        not isinstance(note,str) or len(note.strip())<20):
        raise ValueError('Complete authenticated basis review required')
    if not isinstance(source_url,str):
        raise ValueError('Evidence URL required')
    parsed=urlsplit(source_url)
    if (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or
        parsed.port not in (None,443) or any(ch.isspace() for ch in source_url)):
        raise ValueError('Safe HTTPS evidence URL required')
    # The existing PRICE07 review must itself be demonstrably consistent.
    current=load_audited_nutrition(db,[product_code])[(product_code,'basis_unknown')]
    if current['review_id'] != f'p7:{price07_audit_id}':
        raise IncompleteNutrition('Basis must attest the current PRICE07 audit')
    p7=db.execute('''SELECT a.id,a.actor_id,a.evidence_json,n.source
        FROM p7_nutrition_review_audit a JOIN p7_nutrition_review n
          ON n.product_code=a.product_code AND n.actor_id=a.actor_id
        WHERE a.id=? AND a.product_code=? AND a.event='owner_verified' ''',
        (price07_audit_id,product_code)).fetchone()
    if not p7:
        raise IncompleteNutrition('Basis evidence must link exact PRICE07 audit')
    payload={'product_code':product_code,'state':state,'price07_audit_id':price07_audit_id,
             'price07_evidence_sha256':hashlib.sha256(p7[2].encode()).hexdigest(),
             'source_url':source_url,'note':note.strip(),'actor_id':actor_id}
    digest=_sha(payload)
    db.execute('SAVEPOINT r1_basis')
    try:
        if db.execute('SELECT 1 FROM r1_product_basis WHERE product_code=?',(product_code,)).fetchone():
            raise ValueError('Basis has already been reviewed; use a separate revision process')
        db.execute('''INSERT INTO r1_product_basis
          (product_code,state,price07_audit_id,source_url,note,actor_id,evidence_hash)
          VALUES(?,?,?,?,?,?,?)''',(product_code,state,price07_audit_id,source_url,note.strip(),actor_id,digest))
        db.execute('''INSERT INTO r1_product_basis_audit
          (product_code,actor_id,event,evidence_hash)
          VALUES(?,?,'basis_owner_verified',?)''',(product_code,actor_id,digest))
        db.execute('RELEASE SAVEPOINT r1_basis')
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT r1_basis')
        db.execute('RELEASE SAVEPOINT r1_basis')
        raise
    return {'product_code':product_code,'state':state,'status':'basis_owner_verified'}


def load_reviewed_recipe_nutrients(db, product_codes):
    """Give calculator verified records only when both independent audits match."""
    codes=tuple(dict.fromkeys(product_codes))
    raw=load_audited_nutrition(db,codes)
    verified={}
    for code in codes:
        record=raw[(code,'basis_unknown')]
        row=db.execute('''SELECT b.state,b.price07_audit_id,b.source_url,b.note,
            b.actor_id,b.evidence_hash,a.evidence_hash,pa.evidence_json,
            pa.actor_id,n.actor_id
            FROM r1_product_basis b JOIN r1_product_basis_audit a
              ON a.product_code=b.product_code AND a.actor_id=b.actor_id
               AND a.event='basis_owner_verified'
            JOIN p7_nutrition_review_audit pa ON pa.id=b.price07_audit_id
              AND pa.product_code=b.product_code AND pa.event='owner_verified'
            JOIN p7_nutrition_review n ON n.product_code=b.product_code
            WHERE b.product_code=?''',(code,)).fetchone()
        if row is None:
            raise IncompleteNutrition(f'No audited raw/dry/cooked basis: {code}')
        state,p7_id,source,note,actor,digest,audit_digest,p7_evidence,p7_actor,n_actor=row
        payload={'product_code':code,'state':state,'price07_audit_id':p7_id,
                 'price07_evidence_sha256':hashlib.sha256(p7_evidence.encode()).hexdigest(),
                 'source_url':source,'note':note,'actor_id':actor}
        if (state not in STATES or record['review_id'] != f'p7:{p7_id}' or
            digest!=audit_digest or _sha(payload)!=digest or
            p7_actor != n_actor):
            raise IncompleteNutrition(f'Invalid measurement-basis audit: {code}')
        verified[(code,state)]=dict(record,status='verified',review_id=f'{record["review_id"]}:basis:{digest}')
    return verified
