import json
import sqlite3
import unittest

from catalog_bridge16 import preview


class CatalogBridge16Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE pb_products(code TEXT, name TEXT, base_unit TEXT)')
        self.db.execute('CREATE TABLE zb_canonical_products(code TEXT, name TEXT, base_unit TEXT)')
        self.db.execute('CREATE TABLE zr_recipes(ingredients_json TEXT)')
        self.db.executemany('INSERT INTO pb_products VALUES(?,?,?)',
                            [('a', 'Молоко', 'л'), ('b', 'Картофель', 'кг'), ('c', 'Мука', 'кг')])
        self.db.executemany('INSERT INTO zb_canonical_products VALUES(?,?,?)',
                            [('x', 'молоко', 'л'), ('y', 'КАРТОФЕЛЬ', 'шт'), ('z', 'Яблоко', 'кг')])
        self.db.execute('INSERT INTO zr_recipes VALUES(?)',
                        (json.dumps([{'product': 'молоко', 'amount': 100},
                                     {'product': 'Чай', 'amount': 5}], ensure_ascii=False),))

    def tearDown(self):
        self.db.close()

    def test_matches_and_conflicts_without_writes(self):
        before = self.db.total_changes
        result = preview(self.db)
        self.assertEqual(before, self.db.total_changes)
        self.assertTrue(result['schema_ready'])
        self.assertEqual(result['links']['pb_zb_exact_candidates'], 1)
        self.assertEqual(result['links']['pb_zb_unit_conflicts'], 1)
        self.assertEqual(result['links']['recipe_ingredients']['exact_product_name'], 1)
        self.assertEqual(result['links']['recipe_ingredients']['unmatched_name'], 1)
        self.assertEqual(result['links']['unmatched_unique_names'], 1)
        self.assertEqual(result['links']['without_family_variants_occurrences'], 1)

    def test_family_variant_not_assumed_exact(self):
        self.db.execute('INSERT INTO zr_recipes VALUES(?)',
                        (json.dumps([{'product': 'Творог'}, {'product': 'Творог'}], ensure_ascii=False),))
        self.db.execute('INSERT INTO pb_products VALUES(?,?,?)', ('d', 'Творог 5%', 'кг'))
        result = preview(self.db)['links']
        self.assertEqual(result['with_family_variants_occurrences'], 2)
        self.assertEqual(result['unmatched_unique_names'], 2)
        self.assertEqual(result['recipe_ingredients']['exact_product_name'], 1)

    def test_malformed_recipe_and_missing_schema(self):
        self.db.execute('INSERT INTO zr_recipes VALUES(?)', ('{invalid',))
        self.assertEqual(preview(self.db)['links']['recipe_ingredients']['malformed_recipe_json'], 1)
        self.db.execute('DROP TABLE pb_products')
        self.assertFalse(preview(self.db)['schema_ready'])


if __name__ == '__main__':
    unittest.main()
