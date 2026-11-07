import sqlite3
import unittest

from price07_rev2_core import init as init_p7
from price07_nutrition_review import init as init_review,verify_discovered_nutrition
from recipe01_nutrition import IncompleteNutrition,calculate_recipe
from recipe01_verified_basis import (init_basis_schema,verify_product_basis,
                                     load_reviewed_recipe_nutrients)


class BasisReviewTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        init_p7(self.db)
        self.db.execute('''INSERT INTO p7_discovered_products
          (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
          VALUES('NP-RICE','Тестовый рис','test-digest','vkusvill','sku01')''')
        self.db.commit()
        init_review(self.db)
        verify_discovered_nutrition(self.db,code='NP-RICE',actor_id=99,kcal_100g=350,
            protein_100g=7,fat_100g=1,carbs_100g=78,allergens=[],
            source='https://example.com/rice-nutrition',
            note='Проверена полная этикетка продукта по источнику')
        self.db.commit()

    def tearDown(self):self.db.close()

    def approve(self, **kwargs):
        args=dict(product_code='NP-RICE',state='dry_edible',price07_audit_id=1,
                  source_url='https://example.com/rice-label',actor_id=99,
                  note='Подтверждено, что данные относятся к сухой съедобной крупе')
        args.update(kwargs)
        return verify_product_basis(self.db,**args)

    def test_backup_gate_and_missing_basis_fail_closed(self):
        with self.assertRaises(RuntimeError):init_basis_schema(self.db)
        init_basis_schema(self.db,backup_verified=True)
        with self.assertRaises(IncompleteNutrition):
            load_reviewed_recipe_nutrients(self.db,['NP-RICE'])

    def test_owner_review_unlocks_only_exact_state(self):
        init_basis_schema(self.db,backup_verified=True)
        self.approve()
        nutrition=load_reviewed_recipe_nutrients(self.db,['NP-RICE'])
        output=calculate_recipe([dict(product_id='NP-RICE',state='dry_edible',grams=100)],
                                nutrition,servings=2,cooked_yield_g=300)
        self.assertAlmostEqual(175,output['portion'].kcal)
        with self.assertRaises(IncompleteNutrition):
            calculate_recipe([dict(product_id='NP-RICE',state='cooked_edible',grams=100)],
                             nutrition,servings=2,cooked_yield_g=300)

    def test_tampering_and_wrong_p7_audit_fail_closed(self):
        init_basis_schema(self.db,backup_verified=True)
        with self.assertRaises(IncompleteNutrition):self.approve(price07_audit_id=999)
        self.approve()
        with self.assertRaises(ValueError):self.approve()
        self.db.execute("UPDATE r1_product_basis SET state='cooked_edible'")
        with self.assertRaises(IncompleteNutrition):
            load_reviewed_recipe_nutrients(self.db,['NP-RICE'])

    def test_mismatched_price07_review_actor_is_rejected(self):
        init_basis_schema(self.db,backup_verified=True)
        self.approve()
        self.db.execute("UPDATE p7_nutrition_review_audit SET actor_id=111")
        with self.assertRaises(IncompleteNutrition):
            load_reviewed_recipe_nutrients(self.db,['NP-RICE'])

    def test_one_shot_product_codes_match_list(self):
        init_basis_schema(self.db,backup_verified=True)
        self.approve()
        expected=load_reviewed_recipe_nutrients(self.db,['NP-RICE'])
        actual=load_reviewed_recipe_nutrients(self.db,(x for x in ['NP-RICE']))
        self.assertEqual(expected,actual)

    def test_older_basis_cannot_verify_newer_nutrition_audit(self):
        init_basis_schema(self.db,backup_verified=True)
        self.approve()
        self.db.execute('''UPDATE p7_nutrition_review SET kcal_100g=130
            WHERE product_code='NP-RICE' ''')
        self.db.execute('''INSERT INTO p7_nutrition_review_audit
            (product_code,actor_id,event,evidence_json)
            SELECT product_code,actor_id,event,
            replace(evidence_json,'350','130')
            FROM p7_nutrition_review_audit WHERE id=1''')
        with self.assertRaises(IncompleteNutrition):
            load_reviewed_recipe_nutrients(self.db,['NP-RICE'])

    def test_basis_creation_requires_current_nutrition_audit(self):
        init_basis_schema(self.db,backup_verified=True)
        self.db.execute('''UPDATE p7_nutrition_review SET kcal_100g=130
            WHERE product_code='NP-RICE' ''')
        self.db.execute('''INSERT INTO p7_nutrition_review_audit
            (product_code,actor_id,event,evidence_json)
            SELECT product_code,actor_id,event,
            replace(evidence_json,'350','130')
            FROM p7_nutrition_review_audit WHERE id=1''')
        with self.assertRaises(IncompleteNutrition):self.approve(price07_audit_id=1)


if __name__=='__main__':unittest.main()
