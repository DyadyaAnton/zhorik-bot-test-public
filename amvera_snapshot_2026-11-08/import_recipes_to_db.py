# -*- coding: utf-8 -*-
"""Импорт zhorik_recipes_original_v1.json в КОПИЮ базы тем же кодом, что и команда бота /recipes_import (recipe_bank.import_json).

Использование:
    python3 import_recipes_to_db.py <папка_кода> <папка_с_zhorik.sqlite3> [файл.json]
Работайте только с КОПИЕЙ базы."""
import os, sys
from pathlib import Path
code, data = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
blob_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(__file__).resolve().parent / 'zhorik_recipes_original_v1.json'
os.environ.update(DATA_DIR=str(data), DB_PATH=str(data / 'zhorik.sqlite3'), TELEGRAM_BOT_TOKEN='x', SANDBOX_OWNER_ID='1', LOG_LEVEL='CRITICAL')
sys.path[:0] = [str(code), str(Path(__file__).resolve().parent)]
import aiogram_stub; aiogram_stub.install()
from database import connect_db
import recipe_bank
with connect_db() as db:
    before = db.execute('SELECT COUNT(*) FROM zr_recipes WHERE active=1').fetchone()[0]
    changed, batch_id = recipe_bank.import_json(db, blob_path.read_bytes(), append_only=True)
    after = db.execute('SELECT COUNT(*) FROM zr_recipes WHERE active=1').fetchone()[0]
print(f'активных рецептов до: {before}, добавлено или изменено: {changed}, после: {after}')
print('партия для точного отката:', batch_id or 'нет новых записей')
