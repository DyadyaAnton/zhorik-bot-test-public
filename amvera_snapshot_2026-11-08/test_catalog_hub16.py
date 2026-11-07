import sqlite3
import unittest

from catalog_hub16 import summary


class Hub16Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.executescript('''CREATE TABLE pb_products(code TEXT);
            CREATE TABLE pb_observations(id INTEGER);
            CREATE TABLE zr_recipes(id TEXT,active INTEGER);
            CREATE TABLE menus(id INTEGER,owner_id INTEGER);
            CREATE TABLE lists(id INTEGER,owner_id INTEGER);
            CREATE TABLE user_prices(id INTEGER,user_id INTEGER);
            CREATE TABLE nutrition_products(id INTEGER,confidence TEXT);''')
        self.db.executemany('INSERT INTO lists VALUES (?,?)', [(1,1),(2,2)])
        self.db.executemany('INSERT INTO menus VALUES (?,?)', [(1,1),(2,2)])
        self.db.executemany('INSERT INTO nutrition_products VALUES (?,?)', [(1,'reference_book'),(2,'candidate_unverified')])

    def tearDown(self):
        self.db.close()

    def test_private_counts_and_statuses(self):
        changes = self.db.total_changes
        value = summary(self.db, 1)
        self.assertEqual(self.db.total_changes, changes)
        self.assertEqual(value['owner_lists'], 1)
        self.assertEqual(value['menus'], 1)
        self.assertEqual(value['nutrition_reference'], 1)
        self.assertEqual(value['nutrition_candidates'], 1)
        self.assertEqual(value['retailer_skus'], 0)
        self.assertIn('pb_skus', value['missing_tables'])

    def test_invalid_owner_rejected(self):
        for owner in (0, -1, '1', True):
            with self.assertRaises(ValueError):
                summary(self.db, owner)


if __name__ == '__main__':
    unittest.main()
