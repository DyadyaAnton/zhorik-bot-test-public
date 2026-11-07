import json
import sqlite3
import unittest

from price07_rev2_core import init as price07_init
from price07_nutrition_review import init as review_init, verify_discovered_nutrition
from recipe01_nutrition import IncompleteNutrition, calculate_recipe
from recipe01_price07_adapter import load_audited_nutrition


class Price07NutritionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        price07_init(self.db)
        self.db.execute('''INSERT INTO p7_discovered_products
            (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
            VALUES('NP-R01','Тестовое молоко','fingerprint01','vkusvill','sku01')''')
        self.db.commit()
        review_init(self.db)

    def tearDown(self):
        self.db.close()

    def approve(self):
        verify_discovered_nutrition(
            self.db, code='NP-R01',actor_id=1234,kcal_100g=60,
            protein_100g=3.1,fat_100g=2.5,carbs_100g=4.8,
            allergens=['молоко'],source='https://example.com/nutrition',
            note='Вручную сверены значения и полный аллергенный состав')

    def test_no_review_cannot_claim_complete_kbju(self):
        with self.assertRaises(IncompleteNutrition):
            load_audited_nutrition(self.db,['NP-R01'])

    def test_reviewed_food_cannot_assume_unknown_raw_or_cooked_basis(self):
        self.approve()
        records=load_audited_nutrition(self.db,['NP-R01'])
        self.assertEqual(records[('NP-R01','basis_unknown')]['status'],'needs_basis_review')
        for state in ('raw_edible','cooked','basis_unknown'):
            with self.assertRaises(IncompleteNutrition):
                calculate_recipe([{'product_id':'NP-R01','state':state,'grams':200}],
                                 records,servings=2,cooked_yield_g=200)

    def test_edited_review_without_matching_audit_is_rejected(self):
        self.approve()
        self.db.execute("UPDATE p7_nutrition_review SET kcal_100g=700 WHERE product_code='NP-R01'")
        with self.assertRaises(IncompleteNutrition):
            load_audited_nutrition(self.db,['NP-R01'])

    def test_tampered_audit_and_missing_product_fail_closed(self):
        self.approve()
        self.db.execute("UPDATE p7_nutrition_review_audit SET evidence_json=? WHERE product_code='NP-R01'",
                        (json.dumps({'kcal_100g':60}),))
        with self.assertRaises(IncompleteNutrition):
            load_audited_nutrition(self.db,['NP-R01'])
        with self.assertRaises(IncompleteNutrition):
            load_audited_nutrition(self.db,['NP-UNKNOWN'])


if __name__=='__main__':
    unittest.main()
