"""PRICE 01: isolated, additive schema for verified grocery observations.

No network calls, background jobs, price promotion, or integration with menu
calculations occur here. Call ``init_schema(connection)`` explicitly.
"""

from __future__ import annotations

import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS pb_products (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    base_unit TEXT NOT NULL CHECK(base_unit IN ('кг', 'л', 'шт')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pb_skus (
    id INTEGER PRIMARY KEY,
    retailer TEXT NOT NULL CHECK(retailer IN ('magnit', 'vkusvill', 'lenta', 'auchan',
                  'pyaterochka', 'perekrestok', 'dixy', 'yarche')),
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    brand TEXT,
    product_code TEXT REFERENCES pb_products(code),
    match_status TEXT NOT NULL DEFAULT 'review_required'
      CHECK(match_status IN ('review_required', 'auto_verified', 'manually_verified', 'rejected')),
    match_evidence TEXT,
    UNIQUE(retailer, external_id),
    CHECK(product_code IS NULL OR match_status != 'rejected'),
    CHECK(match_status NOT IN ('auto_verified', 'manually_verified') OR product_code IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS pb_observations (
    id INTEGER PRIMARY KEY,
    sku_id INTEGER NOT NULL REFERENCES pb_skus(id),
    region TEXT,
    region_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(region_confirmed IN (0, 1)),
    store_code TEXT,
    region_key TEXT NOT NULL DEFAULT 'unknown',
    store_key TEXT NOT NULL DEFAULT 'unknown',
    normal_price_rub REAL,
    promo_price_rub REAL,
    current_price_rub REAL,
    unit_price_rub REAL,
    base_unit TEXT CHECK(base_unit IN ('кг', 'л', 'шт')),
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'unverified'
      CHECK(review_status IN ('unverified', 'auto_verified', 'manually_verified', 'rejected')),
    CHECK(normal_price_rub IS NULL OR normal_price_rub > 0),
    CHECK(promo_price_rub IS NULL OR promo_price_rub > 0),
    CHECK(current_price_rub IS NULL OR current_price_rub > 0),
    CHECK(unit_price_rub IS NULL OR unit_price_rub > 0),
    CHECK(normal_price_rub IS NOT NULL OR promo_price_rub IS NOT NULL OR current_price_rub IS NOT NULL),
    CHECK(region_confirmed = 0 OR (region IS NOT NULL AND trim(region) != '')),
    CHECK((region_confirmed = 0 AND region_key = 'unknown') OR
          (region_confirmed = 1 AND region_key = region)),
    CHECK((store_code IS NULL AND store_key = 'unknown') OR
          (store_code IS NOT NULL AND store_key = store_code)),
    CHECK(region_key != '' AND store_key != ''),
    CHECK(review_status NOT IN ('auto_verified', 'manually_verified')
          OR (unit_price_rub IS NOT NULL AND base_unit IS NOT NULL)),
    UNIQUE(sku_id, region_key, store_key, observed_at, source)
);

CREATE TABLE IF NOT EXISTS pb_last_known (
    sku_id INTEGER NOT NULL REFERENCES pb_skus(id),
    region_key TEXT NOT NULL,
    store_key TEXT NOT NULL,
    observation_id INTEGER NOT NULL REFERENCES pb_observations(id),
    PRIMARY KEY(sku_id, region_key, store_key)
);

CREATE TABLE IF NOT EXISTS pb_refresh_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    trigger_type TEXT NOT NULL CHECK(trigger_type IN ('scheduled', 'manual')),
    retailer TEXT CHECK(retailer IN ('magnit', 'vkusvill', 'lenta', 'auchan',
                  'pyaterochka', 'perekrestok', 'dixy', 'yarche')),
    category TEXT,
    status TEXT NOT NULL DEFAULT 'started'
      CHECK(status IN ('started', 'completed', 'partial', 'failed')),
    report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_pb_skus_product ON pb_skus(product_code, match_status);
CREATE INDEX IF NOT EXISTS idx_pb_obs_date ON pb_observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_pb_obs_sku ON pb_observations(sku_id, observed_at);

CREATE TRIGGER IF NOT EXISTS pb_verified_observation_insert
BEFORE INSERT ON pb_observations
WHEN NEW.review_status IN ('auto_verified', 'manually_verified')
BEGIN
  SELECT RAISE(ABORT, 'verified observation requires verified canonical SKU')
  WHERE NOT EXISTS (
    SELECT 1 FROM pb_skus WHERE id = NEW.sku_id
      AND product_code IS NOT NULL
      AND match_status IN ('auto_verified', 'manually_verified')
      AND (SELECT base_unit FROM pb_products WHERE code = pb_skus.product_code) = NEW.base_unit
  );
END;

CREATE TRIGGER IF NOT EXISTS pb_verified_observation_update
BEFORE UPDATE OF sku_id, review_status, base_unit ON pb_observations
WHEN NEW.review_status IN ('auto_verified', 'manually_verified')
BEGIN
  SELECT RAISE(ABORT, 'verified observation requires verified canonical SKU')
  WHERE NOT EXISTS (
    SELECT 1 FROM pb_skus WHERE id = NEW.sku_id
      AND product_code IS NOT NULL
      AND match_status IN ('auto_verified', 'manually_verified')
      AND (SELECT base_unit FROM pb_products WHERE code = pb_skus.product_code) = NEW.base_unit
  );
END;

CREATE TRIGGER IF NOT EXISTS pb_sku_downgrade_guard
BEFORE UPDATE OF match_status, product_code ON pb_skus
BEGIN
  SELECT RAISE(ABORT, 'cannot change canonical SKU of verified observation')
  WHERE EXISTS (
    SELECT 1 FROM pb_observations WHERE sku_id = OLD.id
      AND review_status IN ('auto_verified', 'manually_verified')
  ) AND (NEW.match_status NOT IN ('auto_verified', 'manually_verified')
          OR NEW.product_code IS NOT OLD.product_code);
END;

CREATE TRIGGER IF NOT EXISTS pb_last_known_insert_guard
BEFORE INSERT ON pb_last_known
BEGIN
  SELECT RAISE(ABORT, 'last known observation must belong to SKU and scope')
  WHERE NOT EXISTS (
    SELECT 1 FROM pb_observations WHERE id = NEW.observation_id
      AND sku_id = NEW.sku_id AND region_key = NEW.region_key AND store_key = NEW.store_key
  );
END;

CREATE TRIGGER IF NOT EXISTS pb_last_known_update_guard
BEFORE UPDATE ON pb_last_known
BEGIN
  SELECT RAISE(ABORT, 'last known observation must belong to SKU and scope')
  WHERE NOT EXISTS (
    SELECT 1 FROM pb_observations WHERE id = NEW.observation_id
      AND sku_id = NEW.sku_id AND region_key = NEW.region_key AND store_key = NEW.store_key
  );
END;

CREATE TRIGGER IF NOT EXISTS pb_observation_scope_guard
BEFORE UPDATE OF sku_id, region_key, store_key ON pb_observations
WHEN EXISTS (SELECT 1 FROM pb_last_known WHERE observation_id = OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'cannot change scope of last known observation')
  WHERE NEW.sku_id IS NOT OLD.sku_id
     OR NEW.region_key IS NOT OLD.region_key
     OR NEW.store_key IS NOT OLD.store_key;
END;

CREATE TRIGGER IF NOT EXISTS pb_product_unit_guard
BEFORE UPDATE OF base_unit ON pb_products
BEGIN
  SELECT RAISE(ABORT, 'cannot change unit used by verified observations')
  WHERE NEW.base_unit IS NOT OLD.base_unit AND EXISTS (
    SELECT 1 FROM pb_skus s JOIN pb_observations o ON o.sku_id = s.id
    WHERE s.product_code = OLD.code AND o.review_status IN ('auto_verified', 'manually_verified')
  );
END;
"""


def init_schema(connection: sqlite3.Connection) -> None:
    """Create new tables only; leave legacy products/prices untouched.

    CREATE TABLE IF NOT EXISTS is idempotent. No destructive migrations or
    cleanup policy runs as part of startup. SQLite executescript commits any
    preceding transaction: call on a dedicated initialization connection,
    outside active business transactions.
    """
    connection.executescript(SCHEMA)


def upgrade_retailer_scope(connection: sqlite3.Connection) -> bool:
    """Upgrade existing PRICE01 CHECK constraints without losing row identity.

    SQLite cannot ALTER a CHECK constraint. This is a tightly scoped rebuild
    of exactly two existing price tables, with all original IDs and triggers
    preserved, atomic rollback and a foreign-key check before COMMIT.
    Run only on a dedicated connection and after making a SQLite backup.
    """
    if connection.in_transaction:
        raise RuntimeError('Retailer schema upgrade requires no active transaction')
    tables = {}
    for name in ('pb_skus', 'pb_refresh_runs'):
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        if row:
            tables[name] = row[0]
    if not tables:
        return False
    old = "'magnit', 'vkusvill'"
    new = "'magnit', 'vkusvill', 'lenta', 'auchan', 'pyaterochka', 'perekrestok', 'dixy', 'yarche'"
    needs = [name for name, sql in tables.items() if old in sql and "'lenta'" not in sql]
    if not needs:
        return False
    for name in needs:
        if f'CREATE TABLE {name}' not in tables[name]:
            raise RuntimeError(f'Unexpected existing schema for {name}')
    fk = connection.execute('PRAGMA foreign_keys').fetchone()[0]
    connection.execute('PRAGMA foreign_keys=OFF')
    try:
        connection.execute('BEGIN IMMEDIATE')
        # SQLite validates trigger bodies during ALTER TABLE. Temporarily
        # remove every price trigger referring to rebuilt tables, then
        # recreate their exact original SQL after the new tables exist.
        price_triggers = [(name, sql) for name, sql in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL")
            if any(f' {table}' in sql or f'.{table}' in sql for table in needs)]
        for name, _ in price_triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        for name in needs:
            schema = tables[name]
            old_object_sql = [row[0] for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type='index' AND sql IS NOT NULL",
                (name,))]
            temp_name = f'{name}_retailer_upgrade'
            new_sql = schema.replace(f'CREATE TABLE {name}', f'CREATE TABLE {temp_name}', 1)
            new_sql = new_sql.replace(old, new)
            connection.execute(new_sql)
            connection.execute(f'INSERT INTO {temp_name} SELECT * FROM {name}')
            connection.execute(f'DROP TABLE {name}')
            connection.execute(f'ALTER TABLE {temp_name} RENAME TO {name}')
            for statement in old_object_sql:
                connection.execute(statement)
        for _name, statement in price_triggers:
            connection.execute(statement)
        invalid = connection.execute('PRAGMA foreign_key_check').fetchone()
        if invalid:
            raise sqlite3.IntegrityError('Price schema upgrade would break foreign keys')
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute(f'PRAGMA foreign_keys={int(fk)}')
    return True


def reconcile_legacy_catalog(connection: sqlite3.Connection):
    """Read-only crosswalk proposal for historical product names.

    The only automatic candidate is an *exact* normalized name AND unit
    match to the curated canonical catalog. Other rows remain unresolved;
    no historical table, menu, favorite or price is rewritten here.
    """
    from price_b_catalog import seeds

    canonical = {}
    for code, name, _category, unit in seeds():
        key = (' '.join(name.casefold().replace('ё', 'е').split()), unit)
        canonical.setdefault(key, []).append(code)
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'").fetchone()
    result = {'exact': [], 'review_required': []}
    if not exists:
        return result
    for product_id, name, unit in connection.execute('SELECT id,name,unit FROM products ORDER BY id'):
        key = (' '.join(str(name).casefold().replace('ё', 'е').split()), unit)
        codes = canonical.get(key, ())
        if len(codes) == 1:
            result['exact'].append({'legacy_id': product_id, 'canonical_code': codes[0]})
        else:
            result['review_required'].append({'legacy_id': product_id, 'reason':
                                              'ambiguous' if len(codes) > 1 else 'no_exact_match'})
    return result
