"""Curated facts-only Povar.ru sample importer. Never copies article prose.

Run only for a small, permitted sample. It does not crawl pagination or the
whole website. Dependencies: lxml, stdlib; networking is explicit in collect().
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from urllib.parse import urljoin, urlsplit

from lxml import html

from recipe_catalog import _connection
from recipe_web_seed import initialize_web

BASE = 'https://m.povar.ru'
CATEGORIES = (
    ('/list/syrniki/', ['русская', 'европейская'], ['завтрак', 'полдник']),
    ('/list/salat_grecheskiy/', ['греческая', 'средиземноморская'], ['обед', 'ужин']),
    ('/list/plov/', ['узбекская', 'восточная'], ['обед', 'ужин']),
    ('/list/kotlety/', ['европейская'], ['обед', 'ужин']),
    ('/list/borsh/', ['русская', 'восточноевропейская'], ['обед']),
    ('/list/picca/', ['итальянская', 'европейская'], ['обед', 'ужин']),
    ('/list/omlet/', ['европейская'], ['завтрак']),
    ('/list/shashlyk/', ['кавказская'], ['обед', 'ужин']),
)


def _get(path):
    url = urljoin(BASE, path)
    if urlsplit(url).hostname != 'm.povar.ru':
        raise ValueError('Недоверенный адрес')
    req = urllib.request.Request(url, headers={'User-Agent': 'JorikRecipeCurator/1.0 (small public sample)'})
    with urllib.request.urlopen(req, timeout=15) as response:
        if response.status != 200:
            raise ValueError(f'HTTP {response.status}')
        payload = response.read(1_000_001)
        if len(payload) > 1_000_000:
            raise ValueError('Страница слишком большая')
    return html.fromstring(payload.decode('utf-8', errors='replace'))


def _clean(value):
    return ' '.join(value.split())


def parse_recipe_page(doc, source_url, cuisines, meals):
    names = doc.xpath('//h1/text()')
    parts = doc.xpath('//li[@itemprop="recipeIngredient"]')
    if not names or not (2 <= len(parts) <= 60):
        return None
    ingredients = []
    for part in parts:
        name = _clean(' '.join(part.xpath('.//span[contains(concat(" ",normalize-space(@class)," ")," name ")]/text()')))
        amount = _clean(' '.join(part.xpath('.//span[contains(@class,"cnt")]//text()')))
        if name and len(name) <= 130:
            ingredients.append(_clean(f'{name} — {amount}' if amount else name)[:250])
    if len(ingredients) < 2:
        return None
    # No nutrition claim: page's values can refer to a different serving basis.
    return (source_url, 'Povar.ru', _clean(names[0])[:150],
            json.dumps(cuisines, ensure_ascii=False), json.dumps(meals, ensure_ascii=False),
            json.dumps(ingredients, ensure_ascii=False), None, None, None,
            'требует проверки', source_url)


def repair_legacy_unit_suffixes(*, path=None):
    """Clean unit labels erroneously duplicated in ingredient names by older parser."""
    units = ('ст. ложек', 'ст. ложки', 'ч. ложки', 'ч. ложек', 'грамм',
             'штука', 'штуки', 'штук', 'стакан', 'мл', 'кг', 'литр',
             'зубчика', 'зубчик', 'пучок', 'по вкусу')
    fixed = 0
    with _connection(path) as db:
        for record in db.execute('SELECT id,ingredients_json FROM recipe_web_candidates').fetchall():
            source = json.loads(record['ingredients_json'])
            clean = []
            for value in source:
                name, sep, amount = value.partition(' — ')
                if sep:
                    for unit in units:
                        if name.casefold().endswith(' ' + unit) and unit in amount.casefold():
                            name = name[:-len(unit)-1]
                            break
                clean.append(name + sep + amount)
            if clean != source:
                db.execute('UPDATE recipe_web_candidates SET ingredients_json=? WHERE id=?',
                           (json.dumps(clean, ensure_ascii=False), record['id']))
                fixed += 1
    return fixed


def collect(*, path=None, per_category=4, delay=1.0):
    """Collect a bounded representative sample; do not overwrite user edits."""
    if type(per_category) is not int or not 1 <= per_category <= 5:
        raise ValueError('per_category от 1 до 5')
    if delay < 0.5:
        raise ValueError('Пауза слишком мала')
    initialize_web(path)
    counters = {'new': 0, 'skipped': 0, 'errors': 0}
    visited = set()
    for category, cuisines, meals in CATEGORIES:
        try:
            listing = _get(category)
        except (OSError, ValueError):
            counters['errors'] += 1
            continue
        paths = []
        for link in listing.xpath('//a/@href'):
            p = urlsplit(link).path
            if re.fullmatch(r'/recipes/[a-z0-9_-]+-\d+\.html', p) and p not in paths:
                paths.append(p)
            if len(paths) >= per_category:
                break
        for p in paths:
            if p in visited:
                continue
            visited.add(p)
            time.sleep(delay)
            try:
                row = parse_recipe_page(_get(p), urljoin(BASE, p), cuisines, meals)
                if row is None:
                    counters['skipped'] += 1
                    continue
                with _connection(path) as db:
                    before = db.total_changes
                    db.execute('''INSERT OR IGNORE INTO recipe_web_candidates
                       (source_url,source_name,title,cuisines_json,meals_json,ingredients_json,
                       servings,minutes,kcal_per_100g,review_status,instruction_url)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)''', row)
                    counters['new' if db.total_changes > before else 'skipped'] += 1
            except (OSError, ValueError):
                counters['errors'] += 1
        time.sleep(delay)
    return counters


if __name__ == '__main__':
    from recipe_catalog import DEFAULT_PATH
    print(collect(path=DEFAULT_PATH))
