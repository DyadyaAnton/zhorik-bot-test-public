"""Offline PRICE 01 schema smoke and safety tests (no Amvera or network)."""
import sqlite3
import importlib.util
import unittest
from pathlib import Path

from price_base_v1 import init_schema, reconcile_legacy_catalog, upgrade_retailer_scope


class PriceBaseSchemaTests(unittest.TestCase):
    def test_upgrade_old_retailer_schema_preserves_rows_and_triggers(self):
        old_path = Path(__file__).resolve().parent.parent / 'jorik_amvera_price02_candidate' / 'price_base_v1.py'
        if not old_path.exists():
            self.skipTest('Old PRICE02 schema not shipped in isolated patch')
        spec = importlib.util.spec_from_file_location('price01_legacy_for_test', old_path)
        old_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old_module)
        old_module.init_schema(self.db)
        self.db.execute("INSERT INTO pb_skus(retailer,external_id,title) VALUES ('vkusvill','old','Old milk')")
        sku_id = self.db.execute("SELECT id FROM pb_skus").fetchone()[0]
        self.db.execute("INSERT INTO pb_observations(sku_id,current_price_rub,observed_at,source) VALUES (?,?,?,?)",
                        (sku_id, 93, '2026-11-05T11:00:00+00:00', 'old_test'))
        self.db.commit()
        before = self.db.execute("SELECT * FROM pb_observations").fetchall()
        self.assertTrue(upgrade_retailer_scope(self.db))
        self.assertFalse(upgrade_retailer_scope(self.db))
        self.assertEqual(before, self.db.execute("SELECT * FROM pb_observations").fetchall())
        self.assertEqual(self.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(self.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.db.execute("INSERT INTO pb_skus(retailer,external_id,title) VALUES ('lenta','new','New milk')")
        self.db.execute("INSERT INTO pb_refresh_runs(trigger_type,retailer) VALUES ('manual','auchan')")
        self.assertGreaterEqual(self.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'pb_%'").fetchone()[0], 7)

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')

    def tearDown(self):
        self.db.close()

    def test_additive_idempotent_keeps_legacy_data(self):
        self.db.execute('CREATE TABLE prices (id INTEGER PRIMARY KEY, price REAL)')
        self.db.execute('INSERT INTO prices(price) VALUES (125.5)')
        init_schema(self.db)
        init_schema(self.db)
        self.assertEqual(self.db.execute('SELECT price FROM prices').fetchone()[0], 125.5)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pb_products').fetchone()[0], 0)

    def test_verified_sku_needs_canonical_product(self):
        init_schema(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO pb_skus(retailer, external_id, title, match_status) VALUES (?,?,?,?)',
                            ('vkusvill', '77', 'Молоко', 'auto_verified'))
        self.db.execute('INSERT INTO pb_products(code,name,category,base_unit) VALUES (?,?,?,?)',
                        ('milk', 'Молоко', 'Молочное', 'л'))
        self.db.execute('INSERT INTO pb_skus(retailer,external_id,title,product_code,match_status) VALUES (?,?,?,?,?)',
                        ('vkusvill', '77', 'Молоко 1 л', 'milk', 'auto_verified'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO pb_skus(retailer,external_id,title) VALUES (?,?,?)',
                            ('vkusvill', '77', 'Дубликат'))

    def test_unknown_region_and_candidate_not_auto_approved(self):
        init_schema(self.db)
        self.db.execute('INSERT INTO pb_skus(retailer,external_id,title) VALUES (?,?,?)',
                        ('magnit', '12', 'Сыр'))
        self.db.execute('''INSERT INTO pb_observations
            (sku_id,normal_price_rub,promo_price_rub,observed_at,source)
            VALUES (1,150,129,'2026-11-05','probe')''')
        row = self.db.execute('SELECT region,region_confirmed,review_status,normal_price_rub,promo_price_rub FROM pb_observations').fetchone()
        self.assertEqual(row, (None, 0, 'unverified', 150.0, 129.0))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO pb_observations
                (sku_id,normal_price_rub,observed_at,source,review_status)
                VALUES (1,150,'2026-11-06','probe','auto_verified')''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO pb_observations
                (sku_id,normal_price_rub,observed_at,source,region_confirmed)
                VALUES (1,150,'2026-11-06','probe',1)''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO pb_observations
                (sku_id,normal_price_rub,observed_at,source)
                VALUES (1,150,'2026-11-05','probe')''')

    def test_last_known_can_survive_history_pruning(self):
        init_schema(self.db)
        self.db.execute('INSERT INTO pb_skus(retailer,external_id,title) VALUES (?,?,?)',
                        ('vkusvill','1','Кефир'))
        self.db.execute('''INSERT INTO pb_observations(sku_id,normal_price_rub,unit_price_rub,base_unit,observed_at,source)
                           VALUES (1,120,120,'л','2026-10-01','mcp')''')
        self.db.execute('INSERT INTO pb_last_known(sku_id,region_key,store_key,observation_id) VALUES (1,?,?,1)',
                        ('unknown', 'unknown'))
        self.db.execute("DELETE FROM pb_observations WHERE observed_at < '2026-11-01' AND id NOT IN (SELECT observation_id FROM pb_last_known)")
        self.assertEqual(self.db.execute('SELECT count(*) FROM pb_observations').fetchone()[0], 1)

    def test_observation_verification_requires_verified_sku_and_no_downgrade(self):
        init_schema(self.db)
        self.db.execute('INSERT INTO pb_products(code,name,category,base_unit) VALUES (?,?,?,?)',
                        ('milk', 'Молоко', 'Молочное', 'л'))
        self.db.execute('INSERT INTO pb_skus(retailer,external_id,title) VALUES (?,?,?)',
                        ('vkusvill', '1', 'Молоко 1 л'))
        insert = '''INSERT INTO pb_observations
            (sku_id,normal_price_rub,unit_price_rub,base_unit,observed_at,source,review_status)
            VALUES (1,120,120,'л','2026-11-05','mcp','auto_verified')'''
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(insert)
        self.db.execute("UPDATE pb_skus SET product_code='milk', match_status='auto_verified' WHERE id=1")
        self.db.execute(insert)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE pb_skus SET match_status='review_required' WHERE id=1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE pb_skus SET product_code=NULL WHERE id=1")

    def test_cannot_promote_unverified_observation_on_unverified_sku(self):
        init_schema(self.db)
        self.db.execute('INSERT INTO pb_skus(retailer,external_id,title) VALUES (?,?,?)',
                        ('magnit', '1', 'Творог'))
        self.db.execute('''INSERT INTO pb_observations
            (sku_id,normal_price_rub,unit_price_rub,base_unit,observed_at,source)
            VALUES (1,120,400,'кг','2026-11-05','api')''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE pb_observations SET review_status='auto_verified' WHERE id=1")

    def test_verified_region_and_store_scope_must_match_keys(self):
        init_schema(self.db)
        self.db.execute("INSERT INTO pb_skus(retailer, external_id,title) VALUES ('magnit','1','Сыр')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO pb_observations
            (sku_id,normal_price_rub,observed_at,source,region,region_confirmed,region_key)
            VALUES (1,150,'2026-11-05','probe','Москва',1,'spb')''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO pb_observations
            (sku_id,normal_price_rub,observed_at,source,store_code,store_key)
            VALUES (1,150,'2026-11-05','probe','shop-1','shop-2')''')

    def test_last_known_cannot_point_to_other_sku_or_scope(self):
        init_schema(self.db)
        self.db.executemany('INSERT INTO pb_skus(retailer,external_id,title) VALUES (?,?,?)',
                            [('magnit','1','Сыр'),('magnit','2','Молоко')])
        self.db.execute('''INSERT INTO pb_observations(sku_id,normal_price_rub,observed_at,source)
                           VALUES (1,150,'2026-11-05','probe')''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO pb_last_known VALUES(2,'unknown','unknown',1)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO pb_last_known VALUES(1,'Москва','unknown',1)")

    def test_verified_unit_must_match_canonical_unit(self):
        init_schema(self.db)
        self.db.execute("INSERT INTO pb_products(code,name,category,base_unit) VALUES ('milk','Молоко','Молочное','л')")
        self.db.execute("INSERT INTO pb_skus(retailer,external_id,title,product_code,match_status) VALUES ('vkusvill','1','Молоко','milk','auto_verified')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO pb_observations(sku_id,normal_price_rub,unit_price_rub,base_unit,observed_at,source,review_status)
                           VALUES (1,150,150,'кг','2026-11-05','mcp','auto_verified')''')
        self.db.execute('''INSERT INTO pb_observations(sku_id,normal_price_rub,unit_price_rub,base_unit,observed_at,source,review_status)
                           VALUES (1,150,150,'л','2026-11-05','mcp','auto_verified')''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE pb_observations SET base_unit='кг' WHERE id=1")


    def test_legacy_crosswalk_never_guesses_or_mutates(self):
        from price_b_catalog import seeds
        _, canonical_name, _, canonical_unit = seeds()[0]
        self.db.execute('CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT,unit TEXT)')
        self.db.executemany('INSERT INTO products(id,name,unit) VALUES (?,?,?)', [
            (1, canonical_name, canonical_unit),
            (2, canonical_name, 'шт' if canonical_unit != 'шт' else 'кг'),
            (3, 'Несуществующая номенклатура', 'кг')])
        before = list(self.db.execute('SELECT * FROM products ORDER BY id'))
        result = reconcile_legacy_catalog(self.db)
        self.assertEqual(result['exact'][0]['legacy_id'], 1)
        self.assertEqual(result['exact'][0]['canonical_code'], seeds()[0][0])
        self.assertEqual(len(result['review_required']), 2)
        self.assertEqual(before, list(self.db.execute('SELECT * FROM products ORDER BY id')))


if __name__ == '__main__':
    unittest.main()
