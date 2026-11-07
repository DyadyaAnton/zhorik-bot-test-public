"""Explicit, backup-gated additive migration for manual SKU review.

Never invoked during bot startup. Deploy operator must arrange a maintenance
window and request application after independent review of the backup.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from config import settings
from full_db_backup import create_full_backup
from price07_sku_review import init


def apply_after_backup():
    """Create, verify and retain a full SQLite backup before adding audit schema."""
    database_path = Path(settings.db_path)
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    backup_path = create_full_backup()
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise RuntimeError('Verified SQLite backup is unavailable')
    with closing(sqlite3.connect(f'file:{backup_path.resolve().as_posix()}?mode=ro', uri=True)) as backup:
        if backup.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise RuntimeError('SQLite backup integrity check failed')
        required = ('pb_skus', 'pb_products')
        for table in required:
            if not backup.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                                  ('table',table)).fetchone():
                raise RuntimeError(f'Missing required table in backup: {table}')
    # Only additive DDL after verified backup; no candidate data is promoted.
    with closing(sqlite3.connect(str(database_path), timeout=30)) as db:
        with db:
            if db.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise RuntimeError('Source SQLite integrity check failed')
            init(db)
            if not db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?',
                              ('table','p7_sku_review_audit')).fetchone():
                raise RuntimeError('Audit schema not installed')
    return str(backup_path)


if __name__ == '__main__':
    raise SystemExit('Run apply_after_backup() only during a reviewed maintenance window; '
                     'never auto-migrate production at startup.')
