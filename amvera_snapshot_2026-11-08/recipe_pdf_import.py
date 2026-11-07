"""Conservative import of user-provided PDF menu and OCR recipe candidates.

No recipe is promoted to the validated recipe_catalog by OCR alone.
Complete source text is retained in the separate staging tables so the owner
can compare ingredients/calories against the original page before approval.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from recipe_catalog import _connection, _read_connection, initialize

MENU_NAME = 'FITFASHION · Меню 1400 ккал'
BOOK_NAME = 'Н. А. Теленкова · Низкокалорийные блюда (2010)'

_DISH = re.compile(r'^\s*([А-ЯЁ][А-ЯЁ\s0-9«»()\-–,.]+?)\s*(?:\.\s*){3,}(\d[\d/ ]*)\s*(грамм|гр|мл)\b', re.I)
_CALORIES = re.compile(r'\b(\d{2,4})\s*ккал\b', re.I)
_SERVINGS = re.compile(r'\b(\d{1,2})\s*порци', re.I)
_TIME = re.compile(r'\b(?:(\d{1,3})\s*(?:ч\.?|час(?:а|ов)?)\s*(?:(\d{1,3})\s*мин)?|(\d{1,3})\s*мин)', re.I)


def initialize_sources(path=None):
    initialize(path)
    with _connection(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS recipe_source_dishes (
              id INTEGER PRIMARY KEY,
              source_name TEXT NOT NULL,
              source_page INTEGER NOT NULL,
              menu_day INTEGER NOT NULL,
              meal TEXT NOT NULL,
              title TEXT NOT NULL,
              portion_label TEXT NOT NULL,
              portion_g REAL,
              daily_kcal REAL,
              completeness TEXT NOT NULL DEFAULT 'только название и вес порции',
              UNIQUE(source_name,source_page,meal,title,portion_label)
            );
            CREATE TABLE IF NOT EXISTS recipe_ocr_candidates (
              id INTEGER PRIMARY KEY,
              source_name TEXT NOT NULL,
              source_page INTEGER NOT NULL,
              title_ocr TEXT NOT NULL,
              servings_ocr INTEGER,
              minutes_ocr INTEGER,
              kcal_ocr REAL,
              ingredients_ocr_json TEXT NOT NULL DEFAULT '[]',
              full_text_ocr TEXT NOT NULL,
              review_status TEXT NOT NULL DEFAULT 'OCR · требует проверки',
              UNIQUE(source_name,source_page)
            );
        ''')


def parse_menu_pages(text):
    """Read dish names/served weights only; daily kcal is NOT dish kcal."""
    out = []
    for page_no, page in enumerate(text.split('\f'), 1):
        daily = _CALORIES.search(page)
        if not daily or not re.search(r'(?m)^\s*(?:0?[1-9]|1[0-4])\s*$', page):
            continue
        day_match = re.search(r'(?m)^\s*(0?[1-9]|1[0-4])\s*$', page)
        day = int(day_match.group(1))
        meal = ''
        for line in page.splitlines():
            key = re.sub(r'[^а-яё]', '', line.casefold())
            if 'первыйзавтрак' in key:
                meal = 'завтрак'
            elif 'второйзавтрак' in key:
                meal = 'второй завтрак'
            elif key.startswith('обед'):
                meal = 'обед'
            elif key.startswith('полдник'):
                meal = 'полдник'
            elif key.startswith('ужин'):
                meal = 'ужин'
            found = _DISH.search(line)
            if not found or not meal:
                continue
            title = re.sub(r'\s+', ' ', found.group(1)).strip(' .,')
            portion_label = re.sub(r'\s+', '', found.group(2)) + ' ' + found.group(3).lower()
            amount = float(found.group(2).strip()) if found.group(2).strip().isdigit() and found.group(3).lower() in ('гр','грамм') else None
            out.append({'source_page': page_no, 'menu_day': day, 'daily_kcal': float(daily.group(1)),
                        'meal': meal, 'title': title, 'portion_label': portion_label,
                        'portion_g': amount})
    return out


def import_menu(text, *, path=None):
    initialize_sources(path)
    rows = parse_menu_pages(text)
    with _connection(path) as db:
        before = db.total_changes
        db.executemany('''INSERT OR IGNORE INTO recipe_source_dishes
            (source_name,source_page,menu_day,meal,title,portion_label,portion_g,daily_kcal)
            VALUES (?,?,?,?,?,?,?,?)''',
            [(MENU_NAME, r['source_page'], r['menu_day'], r['meal'], r['title'],
              r['portion_label'], r['portion_g'], r['daily_kcal']) for r in rows])
        return db.total_changes - before


def parse_ocr_page(text, page):
    """Keep uncertainties: a title or number from OCR is only a candidate."""
    if not re.search(r'(?:вам\s+потребует|потребуется)', text, re.I):
        return None
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    index = next((i for i, s in enumerate(lines) if 'потребует' in s.casefold()), 9)
    heading = [x for x in lines[:index] if len(x) >= 4 and
               len(re.findall(r'[А-ЯЁ]', x)) >= 3 and
               len(re.findall(r'[а-яё]', x)) <= len(re.findall(r'[А-ЯЁ]', x))]
    title = ' '.join(heading[-3:])[:250] if heading else f'Рецепт со страницы {page}'
    title = re.sub(r'\s+', ' ', title).strip()
    serving = _SERVINGS.search(text)
    minutes = _TIME.search(text)
    kcal = _CALORIES.search(text)
    # OCR may see “100” as “Ю0”: preserve original text and extracted lines.
    ingredients = [x for x in lines if re.match(r'^[✓✔УVv/]?\s*\d+[\d,\.]*\s*(?:г|кг|мл|л|шт\.|морков|луковиц)', x, re.I)]
    minutes_value = (int(minutes.group(1)) * 60 + int(minutes.group(2) or 0)
                     if minutes and minutes.group(1) else
                     int(minutes.group(3)) if minutes else None)
    return {'source_page': page, 'title': title,
            'servings': int(serving.group(1)) if serving else None,
            'minutes': minutes_value,
            'kcal': float(kcal.group(1)) if kcal else None,
            'ingredients': ingredients[:100], 'raw': text}


def import_book_ocr(pages, *, path=None):
    initialize_sources(path)
    candidates = [r for i, text in enumerate(pages, 1)
                  if (r := parse_ocr_page(text, i)) is not None]
    with _connection(path) as db:
        before = db.total_changes
        db.executemany('''INSERT OR IGNORE INTO recipe_ocr_candidates
            (source_name,source_page,title_ocr,servings_ocr,minutes_ocr,kcal_ocr,
             ingredients_ocr_json,full_text_ocr)
             VALUES (?,?,?,?,?,?,?,?)''',
            [(BOOK_NAME, r['source_page'], r['title'], r['servings'],
              r['minutes'], r['kcal'], json.dumps(r['ingredients'], ensure_ascii=False),
              r['raw']) for r in candidates])
        return db.total_changes - before


def source_counts(*, path=None):
    with _read_connection(path) as db:
        web_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recipe_web_candidates'").fetchone()
        return {
            'menu_dishes': db.execute('SELECT COUNT(*) FROM recipe_source_dishes').fetchone()[0],
            'book_candidates': db.execute('SELECT COUNT(*) FROM recipe_ocr_candidates').fetchone()[0],
            'web_candidates': db.execute('SELECT COUNT(*) FROM recipe_web_candidates').fetchone()[0] if web_exists else 0,
        }


def find_source_dishes(*, day=None, meal=None, text=None, limit=50, path=None):
    """Browse imported dishes; their kcal is DAILY, never per dish."""
    clauses, args = [], []
    if day is not None:
        clauses.append('menu_day=?')
        args.append(int(day))
    if meal:
        clauses.append('meal=?')
        args.append(meal)
    if text:
        clauses.append('title LIKE ?')
        args.append('%' + text.replace('%', '\\%').replace('_', '\\_') + '%')
        clauses[-1] += " ESCAPE '\\'"
    sql = ('SELECT id,source_name,source_page,menu_day,meal,title,portion_label,portion_g,'
           'daily_kcal,completeness FROM recipe_source_dishes')
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += ' ORDER BY menu_day,id LIMIT ?'
    with _read_connection(path) as db:
        return [dict(r) for r in db.execute(sql, (*args, max(1, min(int(limit), 200))))]


def find_book_candidates(*, text=None, limit=50, path=None):
    """OCR is unverified. Exclude raw transcription from result by default."""
    sql = ('SELECT id,source_name,source_page,title_ocr,servings_ocr,minutes_ocr,'
           'kcal_ocr,ingredients_ocr_json,review_status FROM recipe_ocr_candidates')
    args = []
    if text:
        sql += ' WHERE title_ocr LIKE ?'
        args.append('%' + text.replace('%', '\\%').replace('_', '\\_') + '%')
        sql += " ESCAPE '\\'"
    sql += ' ORDER BY source_page LIMIT ?'
    with _read_connection(path) as db:
        out = [dict(r) for r in db.execute(sql, (*args, max(1, min(int(limit), 200))))]
    for row in out:
        row['ingredients_ocr'] = json.loads(row.pop('ingredients_ocr_json'))
    return out
