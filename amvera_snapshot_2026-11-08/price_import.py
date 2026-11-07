"""Import verified observations from a CSV with product,retailer,price,observed_at,source,unit."""
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from database import connect_db
from price_catalog import RETAILERS

def import_csv(text):
    rows = list(csv.DictReader(StringIO(text)))
    required = {"product", "retailer", "price", "observed_at", "source", "unit"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("CSV: нужны колонки product,retailer,price,observed_at,source,unit")
    validated=[]
    for number, row in enumerate(rows,2):
        name=(row["product"] or "").strip()
        retailer=(row["retailer"] or "").strip()
        unit=(row["unit"] or "").strip()
        source=(row["source"] or "").strip()
        try:
            price=Decimal(row["price"].replace(",", "."))
            observed=date.fromisoformat(row["observed_at"])
        except (InvalidOperation, ValueError, AttributeError):
            raise ValueError(f"Строка {number}: некорректная цена или дата")
        if not name or retailer not in RETAILERS or price<=0 or price>1000000 or not source.startswith("https://") or unit not in ("кг","л","шт") or observed>date.today():
            raise ValueError(f"Строка {number}: проверьте название, магазин, цену, источник, единицу и дату")
        validated.append((name,retailer,float(price),observed.isoformat(),source,unit))
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for name,retailer,price,observed,source,unit in validated:
            conn.execute("INSERT OR IGNORE INTO products(name,category,unit) VALUES(?,?,?)", (name,"Импорт",unit))
            product_id=conn.execute("SELECT id FROM products WHERE name=?",(name,)).fetchone()[0]
            actual_unit=conn.execute("SELECT unit FROM products WHERE id=?",(product_id,)).fetchone()[0]
            if actual_unit!=unit:
                raise ValueError(f"Несовпадение единиц для {name}: {actual_unit} и {unit}")
            conn.execute("INSERT INTO prices(product_id,retailer,price,observed_at,source) VALUES(?,?,?,?,?) ON CONFLICT(product_id,retailer,observed_at) DO UPDATE SET price=excluded.price,source=excluded.source", (product_id,retailer,price,observed,source))
    return len(validated)
