"""Add-only, owner-reviewed nutrition and allergen evidence for new products.

Not a food safety certification. Initialization is backup-gated externally;
never call init() automatically on a production bot startup. Caller must
authenticate an owner in a private chat before invoking the write API.
"""
from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

SCHEMA = '''
CREATE TABLE IF NOT EXISTS p7_nutrition_review (
  product_code TEXT PRIMARY KEY REFERENCES p7_discovered_products(code),
  kcal_100g REAL NOT NULL,
  protein_100g REAL NOT NULL,
  fat_100g REAL NOT NULL,
  carbs_100g REAL NOT NULL,
  allergens_json TEXT NOT NULL,
  source TEXT NOT NULL,
  note TEXT NOT NULL,
  actor_id INTEGER NOT NULL,
  verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS p7_nutrition_review_audit (
  id INTEGER PRIMARY KEY,
  product_code TEXT NOT NULL,
  actor_id INTEGER NOT NULL,
  event TEXT NOT NULL CHECK(event='owner_verified'),
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS p7_nutrition_review_audit_product
  ON p7_nutrition_review_audit(product_code,id);
'''


def init(db):
    if db.in_transaction:
        raise RuntimeError('Nutrition migration requires a verified backup and no active transaction')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='p7_discovered_products'").fetchone():
        raise RuntimeError('PRICE07 discovered catalog must exist before nutrition migration')
    db.executescript(SCHEMA)


def _validate(code,actor_id,kcal_100g,protein_100g,fat_100g,carbs_100g,allergens,source,note):
    if not isinstance(code,str) or not 1<=len(code)<=100 or not code.startswith('NP'):
        raise ValueError('Invalid discovered product code')
    if type(actor_id) is not int or actor_id<=0:
        raise ValueError('Authenticated owner actor required')
    values=(kcal_100g,protein_100g,fat_100g,carbs_100g)
    if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=limit
           for v,limit in zip(values,(900,100,100,100))):
        raise ValueError('Complete finite KBJU values per 100g required')
    if sum(values[1:])>100.5:
        raise ValueError('Macros exceed edible product mass')
    if type(allergens) is not list or len(allergens)>25 or any(
            not isinstance(a,str) or not 1<=len(a.strip())<=80 for a in allergens):
        raise ValueError('Explicit complete allergen review required (empty list means checked none)')
    normalized=[a.casefold().strip().replace('ё','е') for a in allergens]
    if len(set(normalized))!=len(normalized):
        raise ValueError('Duplicate allergen codes')
    if not isinstance(source,str) or not 12<=len(source)<=500:
        raise ValueError('Nutritional evidence URL required')
    try:
        parsed=urlsplit(source)
        valid=(parsed.scheme=='https' and parsed.hostname and not parsed.username and
               not parsed.password and parsed.port in (None,443) and
               not any(ch.isspace() or ord(ch)<32 for ch in source))
    except ValueError:
        valid=False
    if not valid:
        raise ValueError('Safe HTTPS evidence URL required')
    if not isinstance(note,str) or not 15<=len(note.strip())<=500:
        raise ValueError('Owner review explanation required')
    return normalized


def verify_discovered_nutrition(db, *, code,actor_id,kcal_100g,protein_100g,
                                fat_100g,carbs_100g,allergens,source,note):
    """One-shot manual promotion with audited evidence. Never from an AI guess."""
    normalized=_validate(code,actor_id,kcal_100g,protein_100g,fat_100g,
                         carbs_100g,allergens,source,note)
    evidence={'kcal_100g':kcal_100g,'protein_100g':protein_100g,
              'fat_100g':fat_100g,'carbs_100g':carbs_100g,
              'allergens':normalized,'source':source,'note':note.strip()}
    db.execute('SAVEPOINT p7_review_nutrition')
    try:
        row=db.execute('''SELECT review_status,kbju_verified,allergens_verified
                          FROM p7_discovered_products WHERE code=?''',(code,)).fetchone()
        if row is None or row[0]!='unverified' or row[1] or row[2]:
            raise ValueError('Discovered product not available for first review')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='p7_nutrition_review'").fetchone():
            raise RuntimeError('Backup-gated nutrition migration has not run')
        if db.execute('SELECT 1 FROM p7_nutrition_review WHERE product_code=?',(code,)).fetchone():
            raise ValueError('Already reviewed')
        db.execute('''INSERT INTO p7_nutrition_review
            (product_code,kcal_100g,protein_100g,fat_100g,carbs_100g,
             allergens_json,source,note,actor_id) VALUES(?,?,?,?,?,?,?,?,?)''',
            (code,*map(float,(kcal_100g,protein_100g,fat_100g,carbs_100g)),
             json.dumps(normalized,ensure_ascii=False),source,note.strip(),actor_id))
        changed=db.execute('''UPDATE p7_discovered_products
            SET review_status='verified',kbju_verified=1,allergens_verified=1
            WHERE code=? AND review_status='unverified'
              AND kbju_verified=0 AND allergens_verified=0''',(code,))
        if changed.rowcount!=1:
            raise RuntimeError('Concurrent nutrition review')
        db.execute('''INSERT INTO p7_nutrition_review_audit
            (product_code,actor_id,event,evidence_json) VALUES(?,?,'owner_verified',?)''',
            (code,actor_id,json.dumps(evidence,ensure_ascii=False,sort_keys=True)))
        db.execute('RELEASE SAVEPOINT p7_review_nutrition')
        return {'code':code,'status':'verified','actor_id':actor_id}
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT p7_review_nutrition')
        db.execute('RELEASE SAVEPOINT p7_review_nutrition')
        raise
