"""Stage 0: durable, owner-scoped SKU links and independent observed prices.

No Telegram or network dependency. SQLite connection belongs to the caller.
"""
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

STORE = 'vkusvill'
SOURCE = 'vkusvill_public_mcp'


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _norm(value):
    return ' '.join(str(value or '').casefold().split())


def _cols(db, table):
    return {r[1] for r in db.execute('PRAGMA table_info("'+table+'")')}


def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS product_links (
      owner_id INTEGER NOT NULL, product_name TEXT NOT NULL COLLATE NOCASE,
      store TEXT NOT NULL, candidate_id TEXT NOT NULL, candidate_name TEXT NOT NULL,
      base_unit TEXT NOT NULL CHECK(base_unit IN ('кг','л')),
      pack_amount REAL NOT NULL CHECK(pack_amount>0),
      approved_at TEXT NOT NULL, approved_by INTEGER NOT NULL,
      batch_id TEXT, source TEXT NOT NULL,
      PRIMARY KEY(owner_id,product_name,store,candidate_id));
    CREATE TABLE IF NOT EXISTS price_observations (
      store TEXT NOT NULL, candidate_id TEXT NOT NULL, pack_price REAL NOT NULL CHECK(pack_price>0),
      observed_at TEXT NOT NULL, source TEXT NOT NULL,
      availability_confirmed INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(store,candidate_id,observed_at,source));
    CREATE TABLE IF NOT EXISTS approval_batches (
      id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, source TEXT NOT NULL,
      created_at TEXT NOT NULL, status TEXT NOT NULL,
      checksum TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
      payload TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS approval_batches_lookup ON approval_batches(owner_id,checksum,status);
    CREATE TABLE IF NOT EXISTS approval_migrations (
      name TEXT PRIMARY KEY, completed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS approval_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
      candidate_id TEXT NOT NULL, action TEXT NOT NULL,
      reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    ''')


def _candidate(db, candidate_id, today=None, max_age_days=14):
    from vkusvill_normalize import normalize_candidate
    cols = _cols(db, 'vkusvill_price_candidates')
    if not cols:
        raise ValueError('Таблица кандидатов отсутствует')
    row = db.execute('SELECT * FROM vkusvill_price_candidates WHERE product_id=?', (str(candidate_id),)).fetchone()
    if row is None:
        raise ValueError('Кандидат не найден')
    r = dict(row)
    norm = normalize_candidate(r)
    if norm['unit_price_status'] != 'estimated_public_catalog' or norm['base_unit'] not in ('кг','л') or not norm['pack_amount'] or norm['pack_amount'] <= 0:
        raise ValueError('Неоднозначная или отсутствующая фасовка')
    if r.get('source') and r['source'] != SOURCE:
        raise ValueError('Неподдерживаемый источник')
    observed = date.fromisoformat(str(r['observed_at'])[:10])
    age = ((today or date.today()) - observed).days
    if age < 0 or age > max_age_days:
        raise ValueError('Цена устарела или датирована будущим')
    price = Decimal(str(r['price_rub']))
    if not price.is_finite() or price <= 0:
        raise ValueError('Некорректная цена')
    return {'candidate_id':str(candidate_id), 'candidate_name':r['name'],
            'base_unit':norm['base_unit'], 'pack_amount':norm['pack_amount'],
            'pack_price':str(price), 'observed_at':r['observed_at'], 'source':SOURCE}


def preview_batch(db, owner_id, pairs, source='telegram', today=None, max_age_days=14):
    """Create preview only; never changes product_links or price_observations."""
    if source not in ('telegram','csv','migration'):
        raise ValueError('Недопустимый источник партии')
    normalized = sorted({(_norm(product), str(cid).strip()) for product,cid in pairs})
    if not normalized or any(not p or not c for p,c in normalized):
        raise ValueError('Пустая или некорректная партия')
    checksum = hashlib.sha256(json.dumps([owner_id,STORE,normalized],ensure_ascii=False).encode()).hexdigest()
    init(db)
    existing = db.execute("SELECT id,status,payload FROM approval_batches WHERE owner_id=? AND checksum=? AND status IN ('preview','applied','partial') ORDER BY created_at DESC LIMIT 1",(owner_id,checksum)).fetchone()
    if existing:
        return {'id':existing['id'],'status':existing['status'],'items':json.loads(existing['payload']),'reused':True}
    items=[]
    for product,cid in normalized:
        try:
            candidate=_candidate(db,cid,today,max_age_days)
            items.append({'product':product,'candidate_id':cid,'status':'valid',**candidate})
        except (ValueError,TypeError,KeyError) as exc:
            items.append({'product':product,'candidate_id':cid,'status':'invalid','reason':str(exc)})
    batch_id=uuid4().hex
    db.execute('INSERT INTO approval_batches(id,owner_id,source,created_at,status,checksum,payload) VALUES(?,?,?,?,?,?,?)',
               (batch_id,owner_id,source,_now(),'preview',checksum,json.dumps(items,ensure_ascii=False)))
    db.commit()  # durable preview before separate atomic apply transaction
    return {'id':batch_id,'status':'preview','items':items,'reused':False}


def apply_batch(db, owner_id, batch_id, mode='all', today=None, max_age_days=14):
    """Atomic apply. mode=all rejects any invalid item; mode=valid_only permits partial."""
    if mode not in ('all','valid_only'):
        raise ValueError('Неизвестный режим')
    db.execute('BEGIN IMMEDIATE')
    try:
        batch=db.execute('SELECT * FROM approval_batches WHERE id=? AND owner_id=?',(batch_id,owner_id)).fetchone()
        if not batch or batch['status'] != 'preview':
            raise ValueError('Партия не найдена или уже обработана')
        items=json.loads(batch['payload'])
        valid=[]; errors=[]
        for item in items:
            try:
                fresh=_candidate(db,item['candidate_id'],today,max_age_days)
                if item['status']!='valid' or any((Decimal(str(item[k])) != Decimal(str(fresh[k]))) if k in ('pack_price','pack_amount') else (str(item[k]) != str(fresh[k])) for k in ('candidate_name','base_unit','pack_amount','pack_price','observed_at')):
                    raise ValueError('Данные кандидата изменились после preview')
                valid.append((item,fresh))
            except (ValueError,TypeError,KeyError) as exc:
                errors.append((item,str(exc)))
        if errors and mode=='all':
            raise ValueError('Есть ошибочные позиции: '+', '.join(x[0]['candidate_id'] for x in errors))
        for item,fresh in valid:
            db.execute('''INSERT OR IGNORE INTO product_links
              (owner_id,product_name,store,candidate_id,candidate_name,base_unit,pack_amount,approved_at,approved_by,batch_id,source)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
              (owner_id,item['product'],STORE,item['candidate_id'],fresh['candidate_name'],fresh['base_unit'],fresh['pack_amount'],_now(),owner_id,batch_id,batch['source']))
            inserted=db.execute('SELECT changes()').fetchone()[0]
            db.execute('''INSERT OR IGNORE INTO price_observations
              (store,candidate_id,pack_price,observed_at,source,availability_confirmed) VALUES(?,?,?,?,?,0)''',
              (STORE,item['candidate_id'],fresh['pack_price'],fresh['observed_at'],SOURCE))
            db.execute('INSERT INTO approval_log(batch_id,candidate_id,action,reason,created_at) VALUES(?,?,?,?,?)',
                       (batch_id,item['candidate_id'],'approve','created' if inserted else 'existing',_now()))
        for item,reason in errors:
            db.execute('INSERT INTO approval_log(batch_id,candidate_id,action,reason,created_at) VALUES(?,?,?,?,?)',
                       (batch_id,item['candidate_id'],'reject',reason,_now()))
        status='partial' if errors else 'applied'
        db.execute('UPDATE approval_batches SET status=? WHERE id=?',(status,batch_id))
        db.commit()
        return {'status':status,'approved':len(valid),'rejected':len(errors)}
    except Exception:
        db.rollback()
        raise


def rollback_batch(db, owner_id, batch_id):
    db.execute('BEGIN IMMEDIATE')
    try:
        batch=db.execute('SELECT status FROM approval_batches WHERE id=? AND owner_id=?',(batch_id,owner_id)).fetchone()
        if not batch or batch['status'] not in ('applied','partial'):
            raise ValueError('Партия недоступна для отката')
        count=db.execute('SELECT COUNT(*) FROM product_links WHERE owner_id=? AND batch_id=?',(owner_id,batch_id)).fetchone()[0]
        db.execute('DELETE FROM product_links WHERE owner_id=? AND batch_id=?',(owner_id,batch_id))
        db.execute("UPDATE approval_batches SET status='rolled_back' WHERE id=?",(batch_id,))
        db.execute('INSERT INTO approval_log(batch_id,candidate_id,action,reason,created_at) VALUES(?,?,?,?,?)',
                   (batch_id,'*','rollback',f'deleted_links={count}',_now()))
        db.commit()
        return count
    except Exception:
        db.rollback(); raise


def migrate_legacy(db):
    """Idempotent, preserves original price date. Legacy table remains untouched."""
    init(db)
    needed={'owner_id','product','candidate_id','candidate_name','pack_price','pack_amount','base_unit','observed_at'}
    if db.execute("SELECT 1 FROM approval_migrations WHERE name='legacy_v1'").fetchone():
        return 0
    if not needed.issubset(_cols(db,'vkusvill_approved_prices')):
        return 0
    db.execute('BEGIN IMMEDIATE')
    try:
        if db.execute("SELECT 1 FROM approval_migrations WHERE name='legacy_v1'").fetchone():
            db.commit()
            return 0
        moved=0
        for r in db.execute('SELECT * FROM vkusvill_approved_prices').fetchall():
            r=dict(r)
            if r['base_unit'] not in ('кг','л') or not r['pack_amount'] or not r['pack_price'] or not r['observed_at']:
                continue
            db.execute('''INSERT OR IGNORE INTO product_links
              (owner_id,product_name,store,candidate_id,candidate_name,base_unit,pack_amount,approved_at,approved_by,batch_id,source)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
              (r['owner_id'],r['product'],STORE,str(r['candidate_id']),r['candidate_name'],r['base_unit'],r['pack_amount'],r.get('approved_at') or r['observed_at'],r['owner_id'],None,'migration'))
            moved+=db.execute('SELECT changes()').fetchone()[0]
            db.execute('''INSERT OR IGNORE INTO price_observations
              (store,candidate_id,pack_price,observed_at,source,availability_confirmed) VALUES(?,?,?,?,?,0)''',
              (STORE,str(r['candidate_id']),r['pack_price'],r['observed_at'],r.get('source') or SOURCE))
        db.execute("INSERT INTO approval_migrations(name,completed_at) VALUES('legacy_v1',?)",(_now(),))
        db.commit()
        return moved
    except Exception:
        db.rollback()
        raise


def linked_quotes(db, owner_id):
    """Most recent observation per link, deterministic tie-break; links survive stale prices."""
    if not _cols(db,'product_links') or not _cols(db,'price_observations'):
        return []
    return db.execute('''SELECT l.product_name AS product,l.candidate_name,
          l.pack_amount,l.base_unit,o.pack_price,o.observed_at
      FROM product_links l JOIN price_observations o
        ON o.store=l.store AND o.candidate_id=l.candidate_id
      WHERE l.owner_id=? AND o.source=? AND o.observed_at=(
        SELECT MAX(p.observed_at) FROM price_observations p
        WHERE p.store=l.store AND p.candidate_id=l.candidate_id AND p.source=?)
      ORDER BY l.product_name, l.candidate_id''',(owner_id,SOURCE,SOURCE)).fetchall()
