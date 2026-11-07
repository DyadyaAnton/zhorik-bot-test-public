"""Offline audit and eligibility tests for owner-verified discovered foods."""
import sqlite3
import unittest

from price07_rev2_core import init as price07_init, eligible_for_menu, query_catalog
from price07_nutrition_review import init as review_init, verify_discovered_nutrition
from price07_unified_catalog import catalog_snapshot


class DiscoveredNutritionReviewTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        price07_init(self.db)
        self.db.execute('''INSERT INTO p7_discovered_products
            (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
            VALUES('NP-TEST','Молоко тестовое','digest123','vkusvill','sku-1')''')
        self.db.commit()
        review_init(self.db)

    def tearDown(self):
        self.db.close()

    def approve(self, **changes):
        data=dict(code='NP-TEST',actor_id=1234,kcal_100g=60,protein_100g=3.1,
                  fat_100g=2.5,carbs_100g=4.8,allergens=['молоко'],
                  source='https://example.com/product-label',
                  note='Проверены состав и значения с этикетки товара')
        data.update(changes)
        return verify_discovered_nutrition(self.db,**data)

    def test_unreviewed_and_arbitrarily_flipped_flags_never_unlock_menu(self):
        self.assertFalse(eligible_for_menu(self.db,'NP-TEST'))
        self.db.execute('''UPDATE p7_discovered_products SET
            kbju_verified=1,allergens_verified=1,review_status='verified'
            WHERE code='NP-TEST' ''')
        self.assertFalse(eligible_for_menu(self.db,'NP-TEST'))
        item=next(p for p in catalog_snapshot(self.db,1)['products'] if p['code']=='NP-TEST')
        self.assertEqual('review_required',item['status'])

    def test_explicit_review_requires_complete_macros_allergens_and_source(self):
        with self.assertRaises(ValueError): self.approve(allergens=None)
        with self.assertRaises(ValueError): self.approve(protein_100g=-1)
        with self.assertRaises(ValueError): self.approve(source='')
        with self.assertRaises(ValueError): self.approve(kcal_100g=float('nan'))
        self.assertEqual(0,self.db.execute('SELECT COUNT(*) FROM p7_nutrition_review').fetchone()[0])
        self.assertFalse(eligible_for_menu(self.db,'NP-TEST'))

    def test_approved_record_persists_complete_data_and_audit(self):
        response=self.approve()
        self.assertEqual('verified',response['status'])
        self.assertTrue(eligible_for_menu(self.db,'NP-TEST'))
        self.assertIn(('NP-TEST','Молоко тестовое'),query_catalog(self.db,'NP',20))
        row=self.db.execute('''SELECT kcal_100g,protein_100g,allergens_json,
            source,actor_id FROM p7_nutrition_review WHERE product_code='NP-TEST' ''').fetchone()
        self.assertEqual((60.,3.1,'["молоко"]','https://example.com/product-label',1234),row)
        self.assertEqual(1,self.db.execute('SELECT COUNT(*) FROM p7_nutrition_review_audit').fetchone()[0])
        with self.assertRaises(ValueError): self.approve()
        self.assertEqual(1,self.db.execute('SELECT COUNT(*) FROM p7_nutrition_review_audit').fetchone()[0])


if __name__=='__main__':unittest.main()
