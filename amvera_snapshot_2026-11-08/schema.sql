-- Магазины / точки доставки (цены зависят от адреса и склада)
CREATE TABLE IF NOT EXISTS stores (
    id          INTEGER PRIMARY KEY,
    chain       TEXT NOT NULL,          -- 'chain_a', 'chain_b'
    store_ref   TEXT,                   -- ID магазина/склада во внутреннем API сети
    city        TEXT DEFAULT 'Москва',
    note        TEXT
);

-- Каноническая номенклатура: «куриное филе», «гречка», «молоко 2,5%»
CREATE TABLE IF NOT EXISTS canonical_products (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    category      TEXT,                 -- мясо, крупы, овощи, молочка...
    base_unit     TEXT NOT NULL CHECK (base_unit IN ('g','ml','pcs')),
    g_per_pcs     REAL,                 -- для штучных: сколько грамм в 1 шт (яйцо ~55)
    kcal_100      REAL,                 -- на 100 г/мл
    protein_100   REAL,
    fat_100       REAL,
    carbs_100     REAL,
    keywords      TEXT                  -- через запятую, для сопоставления
);

-- Конкретные товары (SKU) сетей
CREATE TABLE IF NOT EXISTS skus (
    id            INTEGER PRIMARY KEY,
    chain         TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    brand         TEXT,
    ean           TEXT,
    url           TEXT,
    pack_amount   REAL,                 -- размер упаковки в base_unit (г/мл/шт)
    pack_unit     TEXT,                 -- 'g','ml','pcs'
    canonical_id  INTEGER REFERENCES canonical_products(id),
    match_score   REAL,                 -- уверенность сопоставления 0..100
    match_status  TEXT DEFAULT 'auto',  -- auto / manual / rejected
    UNIQUE (chain, external_id)
);

-- История цен (ежедневные снимки)
CREATE TABLE IF NOT EXISTS prices (
    id          INTEGER PRIMARY KEY,
    sku_id      INTEGER NOT NULL REFERENCES skus(id),
    store_id    INTEGER REFERENCES stores(id),
    price       REAL NOT NULL,          -- обычная цена, руб
    promo_price REAL,                   -- цена по акции/карте, если есть
    in_stock    INTEGER DEFAULT 1,
    captured_at TEXT NOT NULL           -- ISO дата/время
);
CREATE INDEX IF NOT EXISTS ix_prices_sku_time ON prices(sku_id, captured_at);

-- Рецепты
CREATE TABLE IF NOT EXISTS recipes (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    meal      TEXT NOT NULL CHECK (meal IN ('breakfast','lunch','dinner','snack')),
    servings  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    recipe_id    INTEGER REFERENCES recipes(id),
    canonical_id INTEGER REFERENCES canonical_products(id),
    amount       REAL NOT NULL,         -- в base_unit канонического продукта, на весь рецепт
    PRIMARY KEY (recipe_id, canonical_id)
);

-- Актуальная цена за базовую единицу для каждого канонического продукта
-- (минимум по подтверждённым SKU за последние 7 дней, в наличии)
CREATE VIEW IF NOT EXISTS v_canonical_price AS
SELECT
    c.id AS canonical_id,
    c.name,
    c.base_unit,
    MIN(COALESCE(p.promo_price, p.price) / s.pack_amount) AS price_per_unit_min,
    -- медиана в SQLite проще считать в Python; здесь — среднее как грубая оценка
    AVG(COALESCE(p.promo_price, p.price) / s.pack_amount) AS price_per_unit_avg
FROM canonical_products c
JOIN skus s   ON s.canonical_id = c.id AND s.match_status != 'rejected' AND s.pack_amount > 0
JOIN prices p ON p.sku_id = s.id AND p.in_stock = 1
WHERE p.captured_at >= datetime('now', '-7 days')
GROUP BY c.id;
