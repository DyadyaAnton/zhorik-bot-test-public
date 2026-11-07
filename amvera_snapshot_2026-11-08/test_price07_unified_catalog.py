import sqlite3
import unittest
import io
from unittest.mock import patch

from price07_rev2_core import init as init_price
from price07_unified_catalog import (catalog_snapshot, export_catalog_txt,
    personal_prices_by_name,search_catalog,catalog_product_detail)


class UnifiedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        init_price(self.db)
        self.db.execute('''CREATE TABLE user_prices(user_id INT,product TEXT,shop TEXT,
            unit TEXT,price REAL,observed_at TEXT)''')
        self.db.executemany('INSERT INTO user_prices VALUES(?,?,?,?,?,?)', [
            (11,'Молоко 2,5%','Магнит','л',115,'2026-11-05'),
            (11,'Молоко 2,5%','Лента','л',120,'2026-11-06'),
            (11,'Редкая специя','Рынок','кг',900,'2026-11-05'),
            (22,'Молоко 2,5%','Тайный магазин','л',1,'2026-11-06'),
        ])

    def tearDown(self):
        self.db.close()

    def test_one_catalog_merges_personal_offers_without_leaking_other_user(self):
        report = catalog_snapshot(self.db, 11)
        self.assertEqual(report['personal_offers_total'], 3)
        self.assertEqual(report['personal_unlinked_products'], 1)
        milk = next(item for item in report['products'] if item['name']=='Молоко 2,5%')
        self.assertEqual(len(milk['personal_offers']), 2)
        self.assertEqual(report['products_total'], 259)
        exported = export_catalog_txt(self.db, 11)
        self.assertIn('Редкая специя', exported)
        self.assertNotIn('Тайный магазин', exported)

    def test_no_false_average_or_auto_verification(self):
        quotes = personal_prices_by_name(self.db, 11)
        milk = quotes[('молоко 2,5%', 'л')]
        self.assertEqual(len(milk), 2)
        self.assertEqual({v['quality'] for v in milk}, {'personal'})
        self.assertNotIn('average', str(milk))

    def test_search_and_product_details_never_leak_another_users_prices(self):
        matched,total=search_catalog(self.db,11,'молоко')
        self.assertGreater(total,0)
        milk=next(item for item in matched if item['name']=='Молоко 2,5%')
        detail=catalog_product_detail(self.db,11,milk['code'])
        self.assertEqual({p['shop'] for p in detail['product']['personal_offers']},
                         {'Лента','Магнит'})
        self.assertNotIn('Тайный магазин',str(detail))
        other=catalog_product_detail(self.db,22,milk['code'])
        self.assertEqual({p['shop'] for p in other['product']['personal_offers']},
                         {'Тайный магазин'})
        with self.assertRaises(KeyError):
            catalog_product_detail(self.db,22,'personal:редкая специя:кг')

    def test_search_paging_keeps_deterministic_total_without_overlap(self):
        first,total=search_catalog(self.db,11,'молоко',limit=3,offset=0)
        second,again=search_catalog(self.db,11,'молоко',limit=3,offset=3)
        self.assertEqual(total,again)
        self.assertGreater(total,3)
        self.assertFalse({p['code'] for p in first} & {p['code'] for p in second})
        with self.assertRaises(ValueError):
            search_catalog(self.db,11,'молоко',offset=-1)

    def test_product_detail_history_unverified_not_promoted(self):
        self.db.execute('''INSERT INTO pb_skus(retailer,external_id,title,product_code,match_status)
                           VALUES('lenta','sku-2','Молоко 2,5% 950мл','M001','review_required')''')
        # Resolve canonical identity from the fixture instead of relying on codes.
        code=next(row['code'] for row in catalog_snapshot(self.db,11)['products']
                  if row['name']=='Молоко 2,5%')
        self.db.execute("UPDATE pb_skus SET product_code=? WHERE external_id='sku-2'",(code,))
        sku=self.db.execute("SELECT id FROM pb_skus WHERE external_id='sku-2'").fetchone()[0]
        self.db.execute('''INSERT INTO pb_observations(sku_id,current_price_rub,
             unit_price_rub,base_unit,observed_at,source,store_code,store_key)
             VALUES(?,99.99,105.25,'л','2026-11-07T01:00:00+00:00',
                    'lenta_public_probe','4515','4515')''',(sku,))
        history=catalog_product_detail(self.db,11,code)['history']
        self.assertEqual('review_required',history[0]['quality'])
        self.assertIsNone(history[0]['region'])

    def test_unlinked_sku_title_match_is_not_falsely_counted_as_product_history(self):
        self.db.execute('''INSERT INTO p7_discovered_products
            (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
            VALUES('D-TEST','Экспериментальный продукт','fingerprint-test','lenta','sku-3')''')
        self.db.execute('''INSERT INTO pb_skus(retailer,external_id,title,match_status)
            VALUES('lenta','sku-3','Экспериментальный продукт','review_required')''')
        sku=self.db.execute("SELECT id FROM pb_skus WHERE external_id='sku-3'").fetchone()[0]
        self.db.execute('''INSERT INTO pb_observations(sku_id,current_price_rub,
            unit_price_rub,base_unit,observed_at,source,store_code,store_key)
            VALUES(?,90,90,'кг','2026-11-07T01:00:00+00:00',
                   'lenta_public_probe','4515','4515')''',(sku,))
        snap=catalog_snapshot(self.db,11)
        item=next(p for p in snap['products'] if p['code']=='D-TEST')
        self.assertEqual(0,item['retail_offers'])
        self.assertEqual(1,item['potential_unlinked_observations'])
        self.assertEqual(1,snap['retail_unlinked_sku_groups'])
        self.assertEqual([],catalog_product_detail(self.db,11,'D-TEST')['history'])

    def test_invalid_identity_fails_closed(self):
        for invalid in (0, -1, '11', None, True):
            with self.assertRaises(ValueError):
                catalog_snapshot(self.db, invalid)

    def test_market_prices_are_in_one_report_without_false_verification(self):
        self.db.execute('''INSERT INTO pb_skus(retailer,external_id,title,match_status)
                           VALUES('lenta','sku-1','Молоко 2,5% 950мл','review_required')''')
        sku = self.db.execute('SELECT id FROM pb_skus WHERE external_id=?', ('sku-1',)).fetchone()[0]
        self.db.execute('''INSERT INTO pb_observations(sku_id,current_price_rub,
             unit_price_rub,base_unit,observed_at,source,store_code,store_key)
             VALUES(?,99.99,105.25,'л','2026-11-07T01:00:00+00:00',
                    'lenta_public_probe','4515','4515')''', (sku,))
        report = export_catalog_txt(self.db, 11)
        self.assertIn('sku-1', report)
        self.assertIn('99.99 ₽', report)
        self.assertIn('регион не подтверждён', report)
        self.assertIn('unverified', report)

    def test_unified_xlsx_escapes_formula_text(self):
        from openpyxl import load_workbook
        from price_exports import make_unified_xlsx
        self.db.execute('''INSERT INTO user_prices VALUES(11,'=2+2','Рынок','шт',10,'2026-11-06')''')
        class FakeConnection:
            def __enter__(_self): return self.db
            def __exit__(_self,*args): pass
        with patch('database.connect_db', return_value=FakeConnection()):
            binary = make_unified_xlsx(11)
        sheet = load_workbook(io.BytesIO(binary))['Мои предложения']
        cell = next(cell for row in sheet for cell in row
                    if isinstance(cell.value, str) and cell.value.startswith("'=2+2"))
        self.assertEqual(cell.data_type, 's')
        self.assertTrue(str(cell.value).startswith("'="))


if __name__ == '__main__':
    unittest.main()
