"""Актуальная цена за базовую единицу: медиана по SKU в наличии за последние 7 дней."""
import sqlite3, statistics


def canonical_prices(db: sqlite3.Connection, days: int = 7, mode: str = "median") -> dict[int, float]:
    rows = db.execute(
        """SELECT s.canonical_id, COALESCE(p.promo_price, p.price), s.pack_amount, s.pack_unit,
                  c.base_unit, c.g_per_pcs
           FROM prices p JOIN skus s ON s.id = p.sku_id
           JOIN canonical_products c ON c.id = s.canonical_id
           WHERE s.match_status != 'rejected' AND s.pack_amount > 0 AND p.in_stock = 1
             AND p.captured_at >= datetime('now', ?)""", (f"-{days} days",)).fetchall()
    by = {}
    for cid, price, pack, pack_unit, base, g_per_pcs in rows:
        # приводим упаковку к базовой единице продукта (напр. «10 шт» яиц -> граммы)
        if pack_unit == "pcs" and base == "g":
            if not g_per_pcs:
                continue
            pack = pack * g_per_pcs
        elif pack_unit != base:
            continue                      # несовместимые единицы (мл vs г) -> на ручную проверку
        by.setdefault(cid, []).append(price / pack)
    agg = {"median": statistics.median, "min": min}[mode]
    return {cid: agg(v) for cid, v in by.items()}


def recipe_cost(db: sqlite3.Connection, prices: dict[int, float]):
    """Стоимость порции каждого рецепта. Если для ингредиента нет цены — рецепт помечается None."""
    out = {}
    for rid, name, meal, servings in db.execute("SELECT id, name, meal, servings FROM recipes"):
        total, ok = 0.0, True
        kcal = prot = 0.0
        for cid, amount, base, kc, pr in db.execute(
                """SELECT ri.canonical_id, ri.amount, c.base_unit, c.kcal_100, c.protein_100
                   FROM recipe_ingredients ri JOIN canonical_products c ON c.id = ri.canonical_id
                   WHERE ri.recipe_id = ?""", (rid,)):
            if cid not in prices:
                ok = False; break
            total += amount * prices[cid]
            kcal += amount / 100 * (kc or 0)
            prot += amount / 100 * (pr or 0)
        if ok:
            out[rid] = dict(name=name, meal=meal, cost=total / servings,
                            kcal=kcal / servings, protein=prot / servings)
    return out
