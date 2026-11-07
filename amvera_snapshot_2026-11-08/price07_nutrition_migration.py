"""Separate backup-first migration for PRICE07 owner-reviewed nutrition.

Never called during normal startup. Requires an explicit maintenance window
after the base PRICE07 schema has been installed and validated.
"""
import sqlite3
from contextlib import closing
from pathlib import Path

from config import settings
from full_db_backup import create_full_backup
from price07_nutrition_review import init


def apply_after_backup():
    path=Path(settings.db_path)
    if not path.is_file():raise FileNotFoundError(path)
    backup=Path(create_full_backup())
    if not backup.is_file() or backup.stat().st_size==0:
        raise RuntimeError('Verified SQLite backup required')
    with closing(sqlite3.connect(f'file:{backup.resolve().as_posix()}?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()!=('ok',):
            raise RuntimeError('Backup integrity failed')
        if not db.execute('''SELECT 1 FROM sqlite_master WHERE type='table'
                             AND name='p7_discovered_products' ''').fetchone():
            raise RuntimeError('PRICE07 discovered schema absent; abort without mutation')
    with closing(sqlite3.connect(str(path),timeout=30)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()!=('ok',):
            raise RuntimeError('Source integrity failed')
        init(db)
        if not db.execute('''SELECT 1 FROM sqlite_master WHERE type='table'
                             AND name='p7_nutrition_review_audit' ''').fetchone():
            raise RuntimeError('Nutrition audit schema not installed')
    return str(backup)
