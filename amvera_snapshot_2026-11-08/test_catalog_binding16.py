import json
import sqlite3
import unittest

from catalog_binding16 import (active_binding, ingredient_hash, init_schema_on_copy,
                               manually_bind_on_copy)


class CatalogBinding16Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE pb_products(code TEXT PRIMARY KEY, name TEXT)')
        self.db.execute('CREATE TABLE zr_recipes(id TEXT PRIMARY KEY, ingredients_json TEXT)')
        self.db.execute('INSERT INTO pb_products VALUES(?,?)', ('CP0017', 'Творог 5%'))
        self.ingredient = {'product': 'творог', 'amount': 100, 'unit': 'г'}
        self.db.execute('INSERT INTO zr_recipes VALUES(?,?)',
                        ('R1', json.dumps([self.ingredient], ensure_ascii=False)))

    def tearDown(self):
        self.db.close()

    def test_requires_confirmed_copy_and_remains_additive(self):
        with self.assertRaises(PermissionError):
            init_schema_on_copy(self.db)
        init_schema_on_copy(self.db, verified_backup=True)
        self.assertEqual(self.db.execute('SELECT count(*) FROM zr_recipes').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pb_products').fetchone()[0], 1)

    def test_manual_binding_revision_and_event_log(self):
        init_schema_on_copy(self.db, verified_backup=True)
        signature = ingredient_hash(self.ingredient)
        manually_bind_on_copy(self.db, recipe_id='R1', ingredient_index=0,
                              product_code='CP0017', reviewed_by=101, expected_hash=signature)
        self.assertEqual(active_binding(self.db, 'R1', 0), 'CP0017')
        self.assertEqual(self.db.execute('SELECT count(*) FROM zr_ingredient_binding_events16').fetchone()[0], 1)
        self.db.execute('UPDATE zr_recipes SET ingredients_json=? WHERE id=?',
                        (json.dumps([{'product':'творог','amount':120,'unit':'г'}]), 'R1'))
        self.assertIsNone(active_binding(self.db, 'R1', 0))
        with self.assertRaises(ValueError):
            manually_bind_on_copy(self.db, recipe_id='R1', ingredient_index=0,
                                  product_code='CP0017', reviewed_by=101, expected_hash=signature)

    def test_invalid_product_or_reviewer_never_writes(self):
        init_schema_on_copy(self.db, verified_backup=True)
        signature = ingredient_hash(self.ingredient)
        with self.assertRaises(ValueError):
            manually_bind_on_copy(self.db, recipe_id='R1', ingredient_index=0,
                                  product_code='MISSING', reviewed_by=101, expected_hash=signature)
        with self.assertRaises(ValueError):
            manually_bind_on_copy(self.db, recipe_id='R1', ingredient_index=0,
                                  product_code='CP0017', reviewed_by=0, expected_hash=signature)
        self.assertEqual(self.db.execute('SELECT count(*) FROM zr_ingredient_binding_events16').fetchone()[0], 0)

    def test_audit_log_failure_rolls_back_binding_even_in_autocommit(self):
        self.db.close()
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE pb_products(code TEXT PRIMARY KEY, name TEXT)')
        self.db.execute('CREATE TABLE zr_recipes(id TEXT PRIMARY KEY, ingredients_json TEXT)')
        self.db.execute('INSERT INTO pb_products VALUES(?,?)', ('CP0017', 'Творог 5%'))
        self.db.execute('INSERT INTO zr_recipes VALUES(?,?)',
                        ('R1', json.dumps([self.ingredient], ensure_ascii=False)))
        init_schema_on_copy(self.db, verified_backup=True)
        self.db.execute('''CREATE TRIGGER fail_event BEFORE INSERT ON zr_ingredient_binding_events16
                           BEGIN SELECT RAISE(ABORT, 'event log unavailable'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            manually_bind_on_copy(self.db, recipe_id='R1', ingredient_index=0,
                                  product_code='CP0017', reviewed_by=101,
                                  expected_hash=ingredient_hash(self.ingredient))
        self.assertEqual(self.db.execute('SELECT count(*) FROM zr_ingredient_bindings16').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM zr_ingredient_binding_events16').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
