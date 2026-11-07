"""Stage B isolated staging database and manual canonical-product review.

No write operations target the existing prices, price_observations or P4 tables.
The new tables have their own zb_ prefix. The caller must take a full SQLite
backup before first-time schema initialization and before large imports.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from price_b_catalog import seeds
from product_normalizer import normalize, match_product

MAX_CSV_BYTES = 15_000_000
MAX_ROWS = 50_000
MAX_TITLE = 220

SCHEMA = """
CREATE TABLE IF NOT EXISTS zb_canonical_products (
 code TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 category TEXT NOT NULL, base_unit TEXT NOT NULL CHECK(base_unit IN ('кг','л','шт')),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE IF NOT EXISTS zb_import_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT, checksum TEXT NOT NULL UNIQUE,
 filename TEXT NOT NULL, imported_at TEXT NOT NULL, rows_total INTEGER NOT NULL,
 candidates INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS zb_candidates (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL REFERENCES zb_import_batches(id), line_no INTEGER NOT NULL,
 retailer TEXT NOT NULL, query TEXT NOT NULL, product_id TEXT NOT NULL,
 title TEXT NOT NULL, price_rub TEXT, unit_price_rub TEXT, unit_basis TEXT,
 observed_at TEXT, region TEXT, match_status TEXT NOT NULL,
 normalized_kind TEXT NOT NULL, warnings_json TEXT NOT NULL,
 suggested_code TEXT REFERENCES zb_canonical_products(code),
 review_status TEXT NOT NULL DEFAULT 'pending'
   CHECK(review_status IN ('pending','confirmed','rejected')),
 raw_json TEXT NOT NULL, UNIQUE(batch_id,line_no)
);
CREATE INDEX IF NOT EXISTS zb_candidates_review_idx ON zb_candidates(review_status,id);
CREATE INDEX IF NOT EXISTS zb_candidates_suggest_idx ON zb_candidates(suggested_code,review_status);
CREATE TABLE IF NOT EXISTS zb_review_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL
   REFERENCES zb_candidates(id), owner_id INTEGER NOT NULL,
 action TEXT NOT NULL CHECK(action IN ('confirm','reject')),
 canonical_code TEXT, reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zb_verified_links (
 candidate_id INTEGER PRIMARY KEY REFERENCES zb_candidates(id),
 canonical_code TEXT NOT NULL REFERENCES zb_canonical_products(code),
 reviewer_id INTEGER NOT NULL, reviewed_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('confirmed_regional','confirmed_region_unknown'))
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def schema_exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='zb_canonical_products'").fetchone() is not None


def init(db):
    db.executescript(SCHEMA)
    db.executemany("INSERT OR IGNORE INTO zb_canonical_products(code,name,category,base_unit) VALUES (?,?,?,?)", seeds())
    return db.execute("SELECT COUNT(*) FROM zb_canonical_products").fetchone()[0]


def _text(value, max_len):
    return str(value or "").replace("\x00", "").strip()[:max_len]


def _valid_positive(value):
    try:
        x = Decimal(str(value).replace(",", "."))
        return x if x.is_finite() and x > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _key(value):
    return re.sub(r"\s+", " ", str(value or "").strip().casefold().replace("ё", "е"))


def _suggest(db, query):
    """Exact canonical name only; never assume a brand or fatness is equivalent."""
    key = _key(query)
    for row in db.execute("SELECT code,name FROM zb_canonical_products WHERE active=1"):
        if _key(row[1]) == key:
            return row[0]
    return None


def _suggest_index(db):
    """Build the exact-match lookup once per import, never per CSV row."""
    return {_key(name): code for code, name in db.execute(
        "SELECT code,name FROM zb_canonical_products WHERE active=1")}


def parse_csv(blob):
    if not isinstance(blob, bytes) or len(blob) > MAX_CSV_BYTES:
        raise ValueError("CSV превышает предел 15 МБ")
    try:
        source = blob.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("Нужен CSV UTF-8") from exc
    reader = csv.DictReader(io.StringIO(source))
    if not reader.fieldnames or not {"store", "query", "title", "price_rub"}.issubset(reader.fieldnames):
        raise ValueError("Неверные поля CSV лаборатории")
    rows = []
    for index, row in enumerate(reader, 2):
        if len(rows) >= MAX_ROWS:
            raise ValueError("Больше 50 000 строк")
        if None in row:
            raise ValueError(f"Неверный формат CSV в строке {index}")
        rows.append((index, row))
    if not rows:
        raise ValueError("Пустой CSV")
    return rows


def import_csv(db, blob, filename="retail_batch_results.csv"):
    """Atomic immutable import. Does not auto-confirm or modify production DB."""
    rows = parse_csv(blob)
    digest = hashlib.sha256(blob).hexdigest()
    previous = db.execute("SELECT id,candidates FROM zb_import_batches WHERE checksum=?", (digest,)).fetchone()
    if previous:
        return {"batch_id": previous[0], "added": 0, "already_imported": True, "candidates": previous[1]}
    db.execute("BEGIN IMMEDIATE")
    try:
        suggestions = _suggest_index(db)
        db.execute("INSERT INTO zb_import_batches(checksum,filename,imported_at,rows_total,candidates) VALUES(?,?,?,?,0)",
                   (digest, _text(filename, 180), _now(), len(rows)))
        batch_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        added = 0
        rejected = 0
        for line, row in rows:
            title = _text(row.get("title"), MAX_TITLE)
            price = _valid_positive(row.get("price_rub"))
            if _text(row.get("status"), 20) != "OK" or not title or price is None:
                rejected += 1
                continue
            n = normalize(row)
            match = match_product(n, _text(row.get("query"), 150), "raw_ingredient")
            code = suggestions.get(_key(row.get("query")))
            db.execute("""INSERT INTO zb_candidates(
               batch_id,line_no,retailer,query,product_id,title,price_rub,
               unit_price_rub,unit_basis,observed_at,region,match_status,
               normalized_kind,warnings_json,suggested_code,raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (batch_id,line,_text(row.get("store"), 35),_text(row.get("query"), 150),
                _text(row.get("product_id"), 100),title,str(price),n.unit_price_rub,n.unit_price_basis,
                _text(row.get("observed_at"), 45),_text(row.get("region"), 100),match.status,
                n.product_kind,json.dumps(n.warnings,ensure_ascii=False),code,
                json.dumps(row,ensure_ascii=False,separators=(",", ":"))))
            added += 1
        db.execute("UPDATE zb_import_batches SET candidates=? WHERE id=?", (added, batch_id))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return {"batch_id":batch_id,"added":added,"skipped":rejected,"already_imported":False,"candidates":added}


def stats(db):
    products = db.execute("SELECT COUNT(*) FROM zb_canonical_products WHERE active=1").fetchone()[0]
    result = {"canonical_products": products}
    for status, count in db.execute("SELECT review_status, COUNT(*) FROM zb_candidates GROUP BY review_status"):
        result[status] = count
    result["verified_links"] = db.execute("SELECT COUNT(*) FROM zb_verified_links").fetchone()[0]
    result["import_batches"] = db.execute("SELECT COUNT(*) FROM zb_import_batches").fetchone()[0]
    return result


def pending(db, offset=0, limit=5):
    if not 0 <= offset <= 1_000_000:
        raise ValueError("Некорректная страница")
    return db.execute("""SELECT id,retailer,query,title,price_rub,unit_price_rub,
                      unit_basis,match_status,suggested_code,region
                      FROM zb_candidates WHERE review_status='pending'
                      ORDER BY id LIMIT ? OFFSET ?""", (limit,offset)).fetchall()


def get_candidate(db, candidate_id):
    return db.execute("SELECT * FROM zb_candidates WHERE id=?", (candidate_id,)).fetchone()


def search_catalog(db, query, limit=12):
    query = _text(query, 60)
    if not query:
        return []
    # SQLite LIKE is case-sensitive for Cyrillic; normalize in Python instead.
    key = _key(query)
    return [r for r in db.execute("SELECT code,name,base_unit FROM zb_canonical_products WHERE active=1 ORDER BY name")
            if key in _key(r[1]) or query.upper() == r[0]][:limit]


def review(db, candidate_id, owner_id, action, canonical_code=None):
    if action not in ("confirm", "reject"):
        raise ValueError("Неверное решение")
    db.execute("BEGIN IMMEDIATE")
    try:
        candidate = get_candidate(db, candidate_id)
        if not candidate or candidate["review_status"] != "pending":
            raise ValueError("Кандидат уже обработан или отсутствует")
        if action == "confirm":
            code = _text(canonical_code or candidate["suggested_code"], 30).upper()
            target = db.execute("SELECT code,base_unit FROM zb_canonical_products WHERE code=? AND active=1", (code,)).fetchone()
            if not target:
                raise ValueError("Укажи действующий код канонического продукта")
            if candidate["unit_basis"] != target["base_unit"] or not _valid_positive(candidate["unit_price_rub"]):
                raise ValueError("Нет надёжной нормализованной цены или несовместимые единицы")
            if candidate["match_status"] == "likely_unrelated":
                raise ValueError("Посторонний кандидат: нельзя подтверждать без отдельного исправления сопоставления")
            region = _key(candidate["region"])
            region_status = "confirmed_regional" if region and region not in {"неизвестен", "unknown"} else "confirmed_region_unknown"
            db.execute("INSERT INTO zb_verified_links(candidate_id,canonical_code,reviewer_id,reviewed_at,status) VALUES(?,?,?,?,?)",
                       (candidate_id,code,owner_id,_now(),region_status))
        else:
            code = None
        db.execute("UPDATE zb_candidates SET review_status=? WHERE id=?",
                   ("confirmed" if action == "confirm" else "rejected", candidate_id))
        db.execute("INSERT INTO zb_review_log(candidate_id,owner_id,action,canonical_code,reviewed_at) VALUES(?,?,?,?,?)",
                   (candidate_id,owner_id,action,code,_now()))
        db.execute("COMMIT")
        return {"candidate_id":candidate_id,"action":action,"canonical_code":code}
    except Exception:
        db.execute("ROLLBACK")
        raise
