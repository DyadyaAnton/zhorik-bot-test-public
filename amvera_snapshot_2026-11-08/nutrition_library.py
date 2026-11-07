"""Build 18.1: additive nutrition/recipe/plan schema; no unverified source data seeded."""
from database import connect_db

DDL = """
CREATE TABLE IF NOT EXISTS nutrition_products (
 id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL, preparation TEXT NOT NULL DEFAULT 'raw',
 category TEXT NOT NULL DEFAULT '', kcal_100g REAL, protein_100g REAL,
 fat_100g REAL, carbs_100g REAL, source TEXT NOT NULL DEFAULT '',
 source_page TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT 'unverified',
 UNIQUE(canonical_name, preparation),
 CHECK(kcal_100g IS NULL OR kcal_100g >= 0),
 CHECK(protein_100g IS NULL OR protein_100g >= 0),
 CHECK(fat_100g IS NULL OR fat_100g >= 0),
 CHECK(carbs_100g IS NULL OR carbs_100g >= 0)
);
CREATE TABLE IF NOT EXISTS nutrition_recipes (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
 servings REAL, cooked_yield_g REAL, instructions TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT '', source_page TEXT NOT NULL DEFAULT '',
 stated_kcal REAL, stated_protein REAL, stated_fat REAL, stated_carbs REAL,
 stated_basis TEXT NOT NULL DEFAULT 'per_serving',
 status TEXT NOT NULL DEFAULT 'needs_review',
 source_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS nutrition_recipe_ingredients (
 id INTEGER PRIMARY KEY, recipe_id INTEGER NOT NULL REFERENCES nutrition_recipes(id) ON DELETE CASCADE,
 product_id INTEGER REFERENCES nutrition_products(id),
 source_name TEXT NOT NULL, amount REAL, unit TEXT NOT NULL DEFAULT 'г',
 normalized_grams REAL, normalization_status TEXT NOT NULL DEFAULT 'unknown',
 position INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nutrition_plans (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, days_count INTEGER NOT NULL,
 source TEXT NOT NULL DEFAULT '', source_key TEXT UNIQUE,
 status TEXT NOT NULL DEFAULT 'needs_review'
);
CREATE TABLE IF NOT EXISTS nutrition_plan_meals (
 id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES nutrition_plans(id) ON DELETE CASCADE,
 day_number INTEGER NOT NULL, meal_name TEXT NOT NULL, recipe_id INTEGER REFERENCES nutrition_recipes(id),
 source_dish_name TEXT NOT NULL DEFAULT '', portion_g REAL,
 stated_kcal REAL, stated_protein REAL, stated_fat REAL, stated_carbs REAL,
 CHECK(day_number >= 1)
);
CREATE INDEX IF NOT EXISTS idx_nutrition_products_name ON nutrition_products(canonical_name);
CREATE INDEX IF NOT EXISTS idx_nutrition_recipes_category ON nutrition_recipes(category);
CREATE INDEX IF NOT EXISTS idx_nutrition_meals_plan ON nutrition_plan_meals(plan_id,day_number);
"""

def init():
    with connect_db() as db:
        db.executescript(DDL)

def count():
    with connect_db() as db:
        return {key: db.execute('SELECT count(*) FROM nutrition_'+key).fetchone()[0]
                for key in ('products','recipes','plans')}

def find_products(term='',limit=8):
    with connect_db() as db:
        return db.execute('SELECT * FROM nutrition_products WHERE canonical_name LIKE ? ORDER BY canonical_name LIMIT ?',
                          ('%'+term+'%',limit)).fetchall()
