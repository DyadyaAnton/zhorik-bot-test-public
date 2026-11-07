"""Price storage and readable TXT interchange. Existing SQLite rows are preserved."""
import csv
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from database import connect_db
from product_names import canonical

UNITS = {'кг','л','шт','уп','бут','пач'}
HEADER = 'ТОВАР | ЦЕНА | ЕД. | МАГАЗИН | ДАТА'

def init():
    with connect_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS user_prices (
          user_id INTEGER NOT NULL, product TEXT NOT NULL COLLATE NOCASE,
          shop TEXT NOT NULL COLLATE NOCASE, unit TEXT NOT NULL,
          price REAL NOT NULL CHECK(price>0), observed_at TEXT NOT NULL,
          PRIMARY KEY(user_id,product,shop,unit))''')

def get_rows(uid):
    with connect_db() as db:
        rows = [dict(r) for r in db.execute('SELECT product,shop,unit,price,observed_at FROM user_prices WHERE user_id=? ORDER BY product,shop',(uid,))]
    # Preserve every shop and unit, collapse only aliases with the same source.
    merged = {}
    for r in rows:
        r['product'] = canonical(r['product'])
        key = (r['product'].casefold(),r['shop'].casefold(),r['unit'])
        previous = merged.get(key)
        if previous is None or (r['observed_at'], r['product']) > (previous['observed_at'],previous['product']):
            merged[key] = r
    return sorted(merged.values(),key=lambda r:(r['product'].casefold(),r['shop'].casefold()))

def category_for(uid,product):
    with connect_db() as db:
        r=db.execute('SELECT category FROM user_product_categories WHERE user_id=? AND product=? COLLATE NOCASE',(uid,product)).fetchone()
        if r:return r[0]
        r=db.execute('SELECT category FROM products WHERE name=? COLLATE NOCASE',(product,)).fetchone()
        return r[0] if r else 'Иное'

def export_txt(uid):
    rows=get_rows(uid)
    groups={}
    for r in rows:groups.setdefault(category_for(uid,r['product']),[]).append(r)
    lines=['ЖОРИК — МОИ ЦЕНЫ','Формат: товар | цена | единица | магазин | дата (ГГГГ-ММ-ДД)','Измените строки и загрузите TXT обратно. Отсутствующие строки не удаляют старые цены.','']
    for cat in sorted(groups,key=str.casefold):
        lines.extend([f'[{cat}]',HEADER])
        for r in groups[cat]:
            lines.append(f"{r['product']} | {r['price']:.2f} | {r['unit']} | {r['shop']} | {r['observed_at']}")
        lines.append('')
    if not rows:lines.append('Цен пока нет. Добавьте строки под заголовком [Иное].')
    return '\n'.join(lines).rstrip()+'\n'

def _validate(product,shop,unit,price,dt):
    product=canonical(product);shop=shop.strip();unit=unit.strip()
    amount=Decimal(str(price).strip().replace(',','.'))
    day=date.fromisoformat(str(dt).strip()[:10])
    if not (1<=len(product)<=100 and 1<=len(shop)<=80 and unit in UNITS and 0<amount<=1000000 and day<=date.today()):
        raise ValueError('Неверное название, магазин, единица, цена или дата')
    return (product,shop,unit,float(amount),day.isoformat())

def parse_upload(data):
    text=data.decode('utf-8-sig')
    rows=[];errors=[];catalog=[]
    # Backward compatible legacy semicolon CSV.
    if text.lstrip().startswith('product;shop;unit;price;observed_at'):
        reader=csv.DictReader(io.StringIO(text),delimiter=';')
        for line,r in enumerate(reader,2):
            try:
                rows.append(_validate(*(r[k] or '' for k in ('product','shop','unit','price','observed_at'))))
                cat=(r.get('category') or r.get('Категория') or '').strip()
                if cat:catalog.append((canonical(r['product']),cat,r['unit']))
            except (ValueError,InvalidOperation,TypeError,KeyError):errors.append(line)
    else:
        cat='Иное'
        for line,raw in enumerate(text.splitlines(),1):
            s=raw.strip()
            if not s or s.startswith(('ЖОРИК —','Формат:','Измените строки','Цен пока нет.')) or s==HEADER:continue
            if s.startswith('[') and s.endswith(']'):
                cat=s[1:-1].strip()
                if not 2<=len(cat)<=60:errors.append(line);cat='Иное'
                continue
            parts=[p.strip() for p in s.split('|')]
            if len(parts)!=5:errors.append(line);continue
            try:
                product,price,unit,shop,dt=parts
                row=_validate(product,shop,unit,price,dt)
                rows.append(row);catalog.append((row[0],cat,row[2]))
            except (ValueError,InvalidOperation,TypeError):errors.append(line)
    if len(rows)>1000:raise ValueError('Максимум 1000 цен за загрузку')
    # Avoid duplicates inside the file; last occurrence wins.
    unique={}
    for r in rows:unique[(r[0].casefold(),r[1].casefold(),r[2])]=r
    return list(unique.values()),errors,catalog

def apply(uid,rows):
    # Keep other stores, do not delete omitted rows. Normalize old aliases in-place
    # only for the exact same shop/unit, with a transaction and latest date wins.
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in rows:
            product,shop,unit,price,dt=row
            product=canonical(product)
            old=db.execute('SELECT product,observed_at FROM user_prices WHERE user_id=? AND shop=? COLLATE NOCASE AND unit=?',(uid,shop,unit)).fetchall()
            for prev in old:
                if canonical(prev['product']).casefold()==product.casefold() and prev['product'].casefold()!=product.casefold():
                    db.execute('DELETE FROM user_prices WHERE user_id=? AND product=? AND shop=? AND unit=?',(uid,prev['product'],shop,unit))
            db.execute('''INSERT INTO user_prices(user_id,product,shop,unit,price,observed_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(user_id,product,shop,unit) DO UPDATE SET price=excluded.price,observed_at=excluded.observed_at''',(uid,product,shop,unit,price,dt))
    return len(rows)

def apply_categories(uid,catalog):
    from reference_prices import init as ensure_reference_tables
    ensure_reference_tables()
    cleaned={}
    for name,cat,unit in catalog:
        name=canonical(name);cat=cat.strip();unit=unit.strip()
        if 1<=len(name)<=100 and 2<=len(cat)<=60 and unit in UNITS:cleaned[name.casefold()]=(name,cat,unit)
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.executemany('INSERT OR IGNORE INTO user_categories(user_id,name) VALUES(?,?)',[(uid,cat) for name,cat,unit in cleaned.values()])
        db.executemany('''INSERT INTO user_product_categories(user_id,product,category,unit) VALUES(?,?,?,?)
          ON CONFLICT(user_id,product) DO UPDATE SET category=excluded.category,unit=excluded.unit''',[(uid,name,cat,unit) for name,cat,unit in cleaned.values()])
    return len(cleaned)
