"""Human-curated TXT recipe candidates for the independent Жорик catalog.

Format: UTF-8 text, magic header on its own line, then one JSON object per
line. Imported records are NEVER promoted to approved canonical recipes.
"""
from __future__ import annotations

import json
import secrets
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from recipe_catalog import DEFAULT_PATH, _connection, _read_connection

HEADER = 'JORIK_RECIPE_CANDIDATES_V1'
MAX_BYTES = 3 * 1024 * 1024
MAX_ROWS = 100
MAX_INGREDIENTS = 40
FIELDS = {'id', 'title', 'source', 'page', 'cuisines', 'meals', 'ingredients'}
BACKUP_DIR = '/data/recipe_catalog_backups'


def parse_txt(data: bytes):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ValueError('Файл слишком большой')
    try:
        content = data.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('Нужен UTF-8 TXT') from exc
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines or lines[0] != HEADER:
        raise ValueError('Неверный заголовок файла рецептов')
    if len(lines) < 2 or len(lines) > MAX_ROWS + 1:
        raise ValueError('Недопустимое число рецептов')
    parsed, seen = [], set()
    for index, line in enumerate(lines[1:], 2):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f'Неверная JSON-строка {index}') from exc
        if type(obj) is not dict or set(obj) != FIELDS:
            raise ValueError(f'Недопустимые поля строки {index}')
        identity = obj['id']
        if type(identity) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{2,99}', identity):
            raise ValueError(f'Неверный код строки {index}')
        if identity in seen:
            raise ValueError(f'Повторный код строки {index}')
        seen.add(identity)
        for key, limit in (('title', 150), ('source', 200)):
            value = obj[key]
            if type(value) is not str or not 1 <= len(value.strip()) <= limit:
                raise ValueError(f'Недопустимое поле {key} строки {index}')
        if type(obj['page']) is not int or not 1 <= obj['page'] <= 100000:
            raise ValueError(f'Недопустимая страница строки {index}')
        for key in ('cuisines', 'meals'):
            values = obj[key]
            if type(values) is not list or len(values) > 12 or any(
                    type(v) is not str or not 1 <= len(v.strip()) <= 60 for v in values):
                raise ValueError(f'Неверное поле {key} строки {index}')
        ingredients = obj['ingredients']
        if type(ingredients) is not list or not 2 <= len(ingredients) <= MAX_INGREDIENTS or any(
                type(x) is not str or not 1 <= len(x.strip()) <= 180 for x in ingredients):
            raise ValueError(f'Неверные ингредиенты строки {index}')
        parsed.append(obj)
    return parsed


def initialize(path=None):
    with _connection(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS recipe_txt_candidates (
          id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          source TEXT NOT NULL,
          source_page INTEGER NOT NULL,
          cuisines_json TEXT NOT NULL,
          meals_json TEXT NOT NULL,
          ingredients_json TEXT NOT NULL,
          review_status TEXT NOT NULL DEFAULT 'требует проверки'
            CHECK(review_status = 'требует проверки')
        )''')


def import_txt(data: bytes, *, path=None, backup_dir=BACKUP_DIR):
    """Validate everything first, insert atomically, retain existing records."""
    rows = parse_txt(data)
    target = Path(path or DEFAULT_PATH)
    if target.is_file():
        backup_folder = Path(backup_dir)
        backup_folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup_path = backup_folder / ('recipe_txt_' + stamp + '_' + secrets.token_hex(4) + '.sqlite3')
        with closing(sqlite3.connect(str(target))) as existing, closing(sqlite3.connect(str(backup_path))) as backup:
            existing.backup(backup)
        with closing(sqlite3.connect(str(backup_path))) as check:
            if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Нарушена целостность резервной копии')
    initialize(path)
    before = 0
    with _connection(path) as db:
        db.execute('BEGIN IMMEDIATE')
        before = db.total_changes
        db.executemany('''INSERT OR IGNORE INTO recipe_txt_candidates
          (id,title,source,source_page,cuisines_json,meals_json,ingredients_json)
          VALUES (?,?,?,?,?,?,?)''', [
            (r['id'], r['title'], r['source'], r['page'],
             json.dumps(r['cuisines'], ensure_ascii=False),
             json.dumps(r['meals'], ensure_ascii=False),
             json.dumps(r['ingredients'], ensure_ascii=False)) for r in rows
        ])
        added = db.total_changes - before
    return {'received': len(rows), 'added': added, 'duplicates': len(rows) - added}


def count_candidates(*, path=None):
    target = Path(path or DEFAULT_PATH)
    if not target.exists():
        return 0
    with _read_connection(target) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return db.execute('SELECT COUNT(*) FROM recipe_txt_candidates').fetchone()[0] if 'recipe_txt_candidates' in tables else 0
