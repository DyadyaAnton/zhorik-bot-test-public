"""Шаблон сборщика цен. Конкретные URL/поля вы находите сами через DevTools -> Network
(см. README) и прописываете в config.yaml. Здесь только каркас: запрос, пагинация,
пауза, ретраи, разбор JSON по путям, запись в SQLite."""
import time, random, sqlite3, datetime as dt
import httpx
import yaml

from normalize import parse_pack


def dig(obj, path: str, default=None):
    """dig(item, 'price.current') -> item['price']['current']; поддерживает индексы: 'images.0.url'."""
    if not path:
        return default
    cur = obj
    for part in path.split("."):
        try:
            cur = cur[int(part)] if isinstance(cur, list) else cur[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return default
    return cur


class JsonCollector:
    def __init__(self, chain_cfg: dict, db: sqlite3.Connection, store_id: int | None = None):
        self.cfg, self.db, self.store_id = chain_cfg, db, store_id
        self.client = httpx.Client(
            headers=chain_cfg.get("headers", {}),
            timeout=20, follow_redirects=True,
            # proxy="http://user:pass@host:port"  # при необходимости российский прокси
        )
        self.chain = chain_cfg["name"]

    def _get(self, url, params):
        for attempt in range(4):
            try:
                r = self.client.get(url, params=params)
                if r.status_code == 429:
                    time.sleep(10 * (attempt + 1)); continue
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError):
                time.sleep(2 ** attempt)
        return None

    def run(self, queries: list[str], max_pages: int = 3):
        m = self.cfg["map"]  # пути к полям в JSON
        now = dt.datetime.now().isoformat(timespec="seconds")
        for q in queries:
            for page in range(1, max_pages + 1):
                params = {k: (v.format(query=q, page=page, store=self.cfg.get("store_ref", ""))
                              if isinstance(v, str) else v)
                          for k, v in self.cfg["params"].items()}
                data = self._get(self.cfg["url"], params)
                items = dig(data, self.cfg["items_path"], []) if data else []
                if not items:
                    break
                for it in items:
                    self._save(it, m, now)
                self.db.commit()
                time.sleep(random.uniform(1.5, 4))   # вежливая пауза

    def _save(self, it, m, now):
        ext_id = dig(it, m["id"])
        name = dig(it, m["name"], "")
        price = dig(it, m["price"])
        if ext_id is None or price is None:
            return
        ext_id = str(ext_id)
        pack, unit = parse_pack(name)
        if m.get("pack_amount"):                      # если API отдаёт вес отдельно
            pack = dig(it, m["pack_amount"], pack)
            unit = dig(it, m.get("pack_unit", ""), unit)
        self.db.execute(
            """INSERT INTO skus (chain, external_id, name, brand, url, pack_amount, pack_unit)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(chain, external_id) DO UPDATE SET name=excluded.name,
                    pack_amount=COALESCE(excluded.pack_amount, skus.pack_amount),
                    pack_unit=COALESCE(excluded.pack_unit, skus.pack_unit)""",
            (self.chain, ext_id, name, dig(it, m.get("brand", "")),
             dig(it, m.get("url", "")), pack, unit))
        sku_id = self.db.execute("SELECT id FROM skus WHERE chain=? AND external_id=?",
                                 (self.chain, ext_id)).fetchone()[0]
        self.db.execute(
            "INSERT INTO prices (sku_id, store_id, price, promo_price, in_stock, captured_at) VALUES (?,?,?,?,?,?)",
            (sku_id, self.store_id, float(price), dig(it, m.get("promo_price", "")),
             1 if dig(it, m.get("in_stock", ""), True) else 0, now))


def load_config(path="config.yaml"):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)
