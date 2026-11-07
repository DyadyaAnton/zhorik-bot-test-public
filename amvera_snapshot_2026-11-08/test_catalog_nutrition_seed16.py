import sqlite3
import unittest

from catalog_nutrition_seed16 import planned_rows, seed_on_copy
from nutrition_library import DDL


class NutritionSeed16Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.execute('CREATE TABLE pb_products(code TEXT PRIMARY KEY,name TEXT)')
        self.db.executemany('INSERT INTO pb_products(code,name) VALUES (?,?)',
                            [(code, title) for code, title, *_ in planned_rows()])
        self.db.executescript(DDL)

    def tearDown(self):
        self.db.close()

    def test_classification_is_exclusive_and_no_fabrication(self):
        rows = planned_rows()
        self.assertEqual(len(rows), 258)
        self.assertEqual(sum(r[4] == 'reference_book' for r in rows), 13)
        self.assertEqual(sum(r[4] == 'candidate_unverified' for r in rows), 144)
        self.assertEqual(sum(r[4] == 'missing' for r in rows), 101)
        self.assertTrue(all(r[3] == (None, None, None, None) for r in rows if r[4] == 'missing'))

    def test_additive_idempotent_and_preserves_reviewed(self):
        with self.assertRaises(PermissionError):
            seed_on_copy(self.db)
        self.db.execute("INSERT INTO nutrition_products(canonical_name,preparation,confidence,kcal_100g) VALUES('Молоко 1,5%','raw','verified',44)")
        report = seed_on_copy(self.db, verified_backup=True)
        self.assertEqual(report['after'], 258)
        self.assertEqual(report['added'], 257)
        self.assertEqual(report['staged_candidates'], 144)
        self.assertEqual(seed_on_copy(self.db, verified_backup=True)['added'], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM nutrition_products WHERE preparation='unspecified'").fetchone()[0], 257)
        row = self.db.execute("SELECT kcal_100g,confidence FROM nutrition_products WHERE canonical_name='Молоко 1,5%'").fetchone()
        self.assertEqual(row, (44, 'verified'))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM nutrition_products WHERE confidence='candidate_unverified' AND kcal_100g IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM zl_nutrition_candidates16').fetchone()[0], 144)

    def test_failure_rolls_back_all_in_autocommit(self):
        self.db.execute("""CREATE TRIGGER nutrition_seed_broken BEFORE INSERT ON nutrition_products
                           WHEN NEW.canonical_name='Молоко 2,5%'
                           BEGIN SELECT RAISE(ABORT, 'seed unavailable'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            seed_on_copy(self.db, verified_backup=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM nutrition_products').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
