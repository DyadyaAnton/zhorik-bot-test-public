"""Regional statistical reference prices and user-specific categories."""
from database import connect_db
from starter_prices import STARTER,OBSERVATIONS,SOURCE,DATE,REGION

def init():
    with connect_db() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS reference_prices(product TEXT NOT NULL,region TEXT NOT NULL,shop TEXT NOT NULL,unit TEXT NOT NULL,price REAL NOT NULL CHECK(price>0),observed_at TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(product,region,shop,observed_at));
        CREATE TABLE IF NOT EXISTS user_categories(user_id INTEGER NOT NULL,name TEXT NOT NULL,PRIMARY KEY(user_id,name));
        CREATE TABLE IF NOT EXISTS user_product_categories(user_id INTEGER NOT NULL,product TEXT NOT NULL COLLATE NOCASE,category TEXT NOT NULL,unit TEXT NOT NULL,PRIMARY KEY(user_id,product));""")
        db.execute('BEGIN IMMEDIATE')
        for category,name,unit in STARTER:
            db.execute('INSERT INTO products(name,category,unit) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET category=excluded.category,unit=excluded.unit',(name,category,unit))
        for name,price,unit in OBSERVATIONS:
            db.execute('INSERT OR IGNORE INTO reference_prices VALUES(?,?,?,?,?,?,?)',(name,REGION,'Росстат',unit,price,DATE,SOURCE))

def categories(uid):
    with connect_db() as db:
        custom=[r[0] for r in db.execute('SELECT name FROM user_categories WHERE user_id=? ORDER BY name',(uid,))]
    return list(dict.fromkeys([r[0] for r in STARTER]+custom))
def add_category(uid,name):
    if not 2<=len(name)<=60:raise ValueError('Название категории: 2–60 символов')
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE');db.execute('INSERT OR IGNORE INTO user_categories VALUES(?,?)',(uid,name))
def prices(uid,category):
    with connect_db() as db:
        row=db.execute('SELECT city FROM user_cities WHERE user_id=?',(uid,)).fetchone()
        city=row[0] if row else None
        shops=[r[0] for r in db.execute('SELECT shop FROM user_shops WHERE user_id=?',(uid,))]
        rows=db.execute('''SELECT name,unit FROM products WHERE category=? AND name NOT IN (SELECT product FROM user_product_categories WHERE user_id=?) UNION SELECT product,unit FROM user_product_categories WHERE user_id=? AND category=? ORDER BY name''',(category,uid,uid,category)).fetchall()
        result=[]
        for r in rows:
            name,unit=r
            # User prices can be compared only if same unit and selected shops.
            if shops:
                q=','.join('?' for _ in shops)
                obs=db.execute('SELECT shop,price,observed_at FROM user_prices WHERE user_id=? AND product=? AND unit=? AND shop IN ('+q+')',(uid,name,unit,*shops)).fetchall()
            else:obs=[]
            if obs:
                value=sum(x['price'] for x in obs)/len(obs)
                result.append((name,unit,value,'личные цены: '+', '.join(x['shop'] for x in obs)))
            else:
                # Regionally sourced figures cannot be misrepresented as local or store averages.
                regional=db.execute('SELECT price,region,observed_at,source FROM reference_prices WHERE product=? AND unit=? AND region=? ORDER BY observed_at DESC LIMIT 1',(name,unit,city or '')).fetchone()
                if regional:result.append((name,unit,regional['price'],f"{regional['region']}, {regional['observed_at']}; {regional['source']}"))
                else:result.append((name,unit,None,'Нет проверенной цены для выбранных магазинов/города'))
        return result
