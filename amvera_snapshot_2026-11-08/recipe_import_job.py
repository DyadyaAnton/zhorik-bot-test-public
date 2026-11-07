"""Owner-triggered, one-shot recipe staging import with recoverable backup.

No Telegram or main application database access. Only the independent
/data/jorik_recipe_catalog.sqlite3 is ever modified.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from recipe_catalog import DEFAULT_PATH, _read_connection
from recipe_catalog_seed import seed
from recipe_source_merge import merge

STAGING_PATH = '/data/recipe_catalog_import.sqlite3'
BACKUP_DIR = '/data/recipe_catalog_backups'
EXPECTED = {'recipe_source_dishes': 112, 'recipe_ocr_candidates': 19,
            'recipe_web_candidates': 36}


class AtomicPublishUnsupported(RuntimeError):
    """The data mount cannot safely publish a new SQLite file."""


def inspect_source(source=STAGING_PATH):
    p = Path(source)
    if not p.is_file():
        raise FileNotFoundError('Импортная база отсутствует')
    with _read_connection(p) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Нарушена целостность импортной базы')
        counts = {}
        for table, expected in EXPECTED.items():
            try:
                value = db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            except sqlite3.Error as exc:
                raise ValueError('Неверная схема импортной базы') from exc
            if value != expected:
                raise ValueError(f'Неверное количество записей в {table}')
            counts[table] = value
        if db.execute("SELECT COUNT(*) FROM recipe_web_candidates WHERE review_status!='требует проверки' OR kcal_per_100g IS NOT NULL").fetchone()[0]:
            raise ValueError('Непроверенные веб-рецепты нельзя импортировать')
    return counts


def run_import(*, source=STAGING_PATH, target=DEFAULT_PATH, backup_dir=BACKUP_DIR):
    """Validate, back up a live catalog, or atomically publish a new catalog."""
    counts = inspect_source(source)
    target_path = Path(target).resolve()
    source_path = Path(source).resolve()
    if target_path == source_path:
        raise ValueError('Нельзя импортировать в исходный файл')
    old_exists = target_path.exists()
    backup_path = None
    if old_exists:
        backup_folder = Path(backup_dir)
        backup_folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup_path = backup_folder / ('recipe_' + stamp + '_' + secrets.token_hex(4) + '.sqlite3')
        with closing(sqlite3.connect(str(target_path))) as existing, closing(sqlite3.connect(str(backup_path))) as backup:
            existing.backup(backup)
        with closing(sqlite3.connect(str(backup_path))) as backup:
            if backup.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Резервная копия не прошла проверку')
        added = merge(str(source_path), target_path=str(target_path))
    else:
        # Do not expose a half-seeded database if the initial import fails.
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix='.recipe_import_', suffix='.sqlite3', dir=target_path.parent)
        os.close(fd)
        temporary = Path(temp_name)
        try:
            seed(path=str(temporary))
            added = merge(str(source_path), target_path=str(temporary))
            # link() is atomic and refuses to overwrite an independently
            # created target, including a target created during the call.
            try:
                os.link(temporary, target_path)
            except FileExistsError:
                raise
            except OSError as exc:
                # No non-atomic copy fallback: a crash could expose a partial DB.
                raise AtomicPublishUnsupported('Хранилище не поддерживает безопасную публикацию SQLite') from exc
        finally:
            temporary.unlink(missing_ok=True)
    return {'source': counts, 'added': added,
            'backup': str(backup_path) if backup_path else None,
            'created_new': not old_exists}
