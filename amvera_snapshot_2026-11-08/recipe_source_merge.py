"""Merge extracted PDF source records into an existing recipe catalog, safely.

Usage: python -m recipe_source_merge /data/recipe_catalog_import.sqlite3
Never overwrites existing canonical recipes, IDs, or source records.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from recipe_catalog import DEFAULT_PATH, _connection, _read_connection
from recipe_web_seed import initialize_web


def merge(source_path, *, target_path=None):
    source = Path(source_path).resolve()
    target = Path(target_path or DEFAULT_PATH).resolve()
    if source == target:
        raise ValueError('Исходная и целевая базы совпадают')
    if not source.is_file():
        raise FileNotFoundError(source)
    initialize_web(target)
    tables = {
        'recipe_source_dishes': ('source_name', 'source_page', 'menu_day', 'meal',
                                 'title', 'portion_label', 'portion_g', 'daily_kcal', 'completeness'),
        'recipe_ocr_candidates': ('source_name', 'source_page', 'title_ocr', 'servings_ocr',
                                  'minutes_ocr', 'kcal_ocr', 'ingredients_ocr_json',
                                  'full_text_ocr', 'review_status'),
        'recipe_web_candidates': ('source_url', 'source_name', 'title', 'cuisines_json', 'meals_json',
                                  'ingredients_json', 'servings', 'minutes', 'kcal_per_100g',
                                  'review_status', 'instruction_url'),
    }
    counts = {}
    with _read_connection(source) as src, _connection(target) as dst:
        # Preflight every table before the first INSERT, so broken staging
        # cannot leave some source categories copied but others missing.
        for table, cols in tables.items():
            for label, db in (('источнике', src), ('приёмнике', dst)):
                actual = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                missing = set(cols) - actual
                if missing:
                    raise ValueError(f'Несовместимая схема {table} в {label}: {sorted(missing)}')
        statuses = {row[0] for row in src.execute('SELECT DISTINCT review_status FROM recipe_web_candidates')}
        if statuses - {'требует проверки'}:
            raise ValueError('Обнаружен недопустимый статус проверки веб-рецептов')
        dst.execute('BEGIN IMMEDIATE')
        for table, cols in tables.items():
            columns = ','.join(cols)
            rows = src.execute(f'SELECT {columns} FROM {table}').fetchall()
            before = dst.total_changes
            dst.executemany(f'INSERT OR IGNORE INTO {table} ({columns}) VALUES (' +
                            ','.join('?' for _ in cols) + ')',
                            [tuple(row) for row in rows])
            counts[table] = dst.total_changes - before
    with _read_connection(target) as check:
        integrity = check.execute('PRAGMA integrity_check').fetchone()[0]
        foreign_keys = check.execute('PRAGMA foreign_key_check').fetchall()
    if integrity != 'ok' or foreign_keys:
        raise RuntimeError('Проверка целостности после импорта не пройдена')
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Безопасное добавление PDF-каталога Жорика')
    parser.add_argument('source', help='путь к SQLite из ZIP')
    parser.add_argument('--target', default=DEFAULT_PATH, help='существующая база в /data')
    args = parser.parse_args()
    print('Добавлено без перезаписи:', merge(args.source, target_path=args.target))
    print('Проверки: integrity_check=ok; foreign_key_check=пусто')
