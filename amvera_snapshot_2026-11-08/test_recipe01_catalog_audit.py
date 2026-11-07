import sqlite3
import unittest

from recipe01_catalog_audit import catalog_readiness


class Recipe01CatalogAuditTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('''CREATE TABLE zr_recipes
            (id TEXT PRIMARY KEY,title TEXT,ingredients_json TEXT,nutrition_status TEXT,active INTEGER);
            CREATE TABLE zr_recipe_yields
            (recipe_id TEXT PRIMARY KEY,cooked_grams REAL,yield_status TEXT);
            INSERT INTO zr_recipes VALUES
            ('R1','Каша','[{"product":"Рис","amount":80,"unit":"г"}]','estimated_unverified',1),
            ('R2','Салат','[]','verified',1);
            INSERT INTO zr_recipe_yields VALUES ('R1',240,'unverified'),('R2',100,'verified');''')

    def tearDown(self):self.db.close()

    def test_flags_never_act_as_ingredient_review(self):
        report=catalog_readiness(self.db)
        self.assertFalse(report['counts']['ready'])
        self.assertEqual(0,report['counts']['eligible_with_verified_kbju'])
        self.assertEqual(100,report['counts']['remaining_to_pilot'])
        self.assertEqual(1,report['counts']['missing_yield'])
        self.assertEqual(1,report['counts']['missing_ingredients'])
        self.assertIn('ingredient_audit_not_connected',report['candidates'][1]['review_reasons'])

    def test_read_only(self):
        before=self.db.total_changes
        catalog_readiness(self.db)
        self.assertEqual(before,self.db.total_changes)


if __name__=='__main__':unittest.main()
