"""Price-only SQLite snapshots. Never restore shopping lists or user settings."""
import sqlite3, gzip, tempfile, shutil, re
from datetime import datetime
from pathlib import Path
from config import settings
from database import connect_db

TABLES=('user_prices','user_categories','user_product_categories','products','prices')
BACKUP_DIR=settings.data_dir/'price_backups'
FULL_BACKUP_DIR=settings.data_dir/'price_full_backups'


def create_full_snapshot(*, destination_dir=None):
    """Make an integrity-checked WAL-safe full snapshot before pb_* migration.

    This is a disaster-recovery artifact, not a request to restore or overwrite
    production. It includes all tables and triggers, unlike price-only export.
    """
    directory=Path(destination_dir) if destination_dir is not None else FULL_BACKUP_DIR
    directory.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
    destination=directory/f'zhorik_full_preprice_{stamp}.sqlite3.gz'
    with tempfile.TemporaryDirectory() as tmp:
        snapshot=Path(tmp)/'snapshot.sqlite3'
        with connect_db() as source,sqlite3.connect(snapshot) as out:
            source.backup(out)
        with sqlite3.connect(snapshot) as verify:
            if verify.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Full SQLite snapshot failed integrity verification')
        temporary=directory/f'{destination.name}.tmp'
        try:
            with snapshot.open('rb') as src,gzip.open(temporary,'wb') as compressed:
                shutil.copyfileobj(src,compressed)
            with gzip.open(temporary,'rb') as compressed:
                header=compressed.read(16)
            if header!=b'SQLite format 3\x00':
                raise RuntimeError('Full SQLite snapshot has invalid header')
            temporary.replace(destination)
        finally:
            if temporary.exists():temporary.unlink()
    return destination

def _safe_tables(db):
    return [t for t in TABLES if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone()]

def _new_snapshot():
    BACKUP_DIR.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
    destination=BACKUP_DIR/f'zhorik_prices_{stamp}.sqlite3.gz'
    with tempfile.TemporaryDirectory() as temp:
        snapshot=Path(temp)/'live.sqlite3'
        # Online snapshot is WAL-safe.
        with connect_db() as source:
            with sqlite3.connect(snapshot) as dest: source.backup(dest)
        output=Path(temp)/'prices.sqlite3'
        with sqlite3.connect(snapshot) as src,sqlite3.connect(output) as out:
            for name in _safe_tables(src):
                schema=src.execute('SELECT sql FROM sqlite_master WHERE type="table" AND name=?',(name,)).fetchone()[0]
                # Price-only snapshots must not carry unrelated user records.
                out.execute(schema)
                cols=[r[1] for r in src.execute(f'PRAGMA table_info({name})')]
                placeholders=','.join('?' for _ in cols)
                out.executemany(f'INSERT INTO {name} VALUES({placeholders})',src.execute(f'SELECT * FROM {name}'))
            out.commit()
            if out.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Ошибка проверки резервной копии')
        with output.open('rb') as f,gzip.open(destination,'wb') as z:shutil.copyfileobj(f,z)
    _rotate()
    return destination

def _rotate():
    for old in sorted(BACKUP_DIR.glob('zhorik_prices_*.sqlite3.gz'),reverse=True)[5:]:old.unlink()

def list_backups():
    if not BACKUP_DIR.exists():return []
    return sorted(BACKUP_DIR.glob('zhorik_prices_*.sqlite3.gz'),reverse=True)[:5]

def create_backup():return _new_snapshot()

def count_prices():
    with connect_db() as db:
        return {t:db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in _safe_tables(db)}

def _test_shop(shop):return bool(re.search(r'тестов|test|демо|demo',shop,re.I))

def clear_prices(test_only=False):
    # Snapshot must succeed before BEGIN IMMEDIATE. No destructive operation without it.
    archive=create_backup()
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            if test_only:
                keys=[(r[0],r[1],r[2],r[3]) for r in db.execute("SELECT user_id,product,shop,unit FROM user_prices") if _test_shop(r[2])]
                db.executemany("DELETE FROM user_prices WHERE user_id=? AND product=? AND shop=? AND unit=?",keys)
                count=len(keys)
            else:
                count=db.execute('SELECT count(*) FROM user_prices').fetchone()[0]
                db.execute('DELETE FROM user_prices')
                # Also remove shared retail observations, not product nomenclature.
                if 'prices' in _safe_tables(db):
                    count+=db.execute('SELECT count(*) FROM prices').fetchone()[0]
                    db.execute('DELETE FROM prices')
            db.commit()
        except Exception:
            db.rollback();raise
    return count,archive

def restore_backup(index):
    files=list_backups()
    if not 0<=index<len(files):raise ValueError('Копия не найдена')
    target=files[index]
    with tempfile.TemporaryDirectory() as tmp:
        extracted=Path(tmp)/'restore.sqlite3'
        with gzip.open(target,'rb') as src,extracted.open('wb') as out:shutil.copyfileobj(src,out)
        with sqlite3.connect(extracted) as src:
            if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Повреждённая копия')
            source_tables=[t for t in _safe_tables(src) if t in ('user_prices','prices')]
            # Restore ONLY price observations; never overwrite product nomenclature or categories.
            # Ensure all expected price tables are present before touching live DB.
            if 'user_prices' not in source_tables:raise ValueError('В копии отсутствует user_prices')
            records={name:list(src.execute(f'SELECT * FROM {name}')) for name in source_tables}
            schemas={name:src.execute('SELECT sql FROM sqlite_master WHERE type="table" AND name=?',(name,)).fetchone()[0] for name in source_tables}
        # Create rollback point before replacement.
        safeguard=create_backup()
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                # Preserve the product catalog: other modules may reference products.id.
                if 'products' in source_tables:
                    for row in records['products']:
                        cols=[r[1] for r in db.execute('PRAGMA table_info(products)')]
                        db.execute(f'INSERT OR IGNORE INTO products ({",".join(cols)}) VALUES ({",".join("?" for _ in cols)})',row)
                for name in ('user_prices','user_categories','user_product_categories','prices'):
                    if name not in source_tables:continue
                    if name not in _safe_tables(db):db.execute(schemas[name])
                    db.execute(f'DELETE FROM {name}')
                    if records[name]:
                        q=','.join('?' for _ in records[name][0]);db.executemany(f'INSERT INTO {name} VALUES({q})',records[name])
                db.commit()
            except Exception:db.rollback();raise
    return target,safeguard
