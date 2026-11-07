import unittest
import sqlite3

from price07_sku_identity import assess, inspect_sku


class SKUIdentityTests(unittest.TestCase):
    def test_exact_title_is_not_verified(self):
        match = assess('Молоко 2,5% 925 мл', 'молоко 2.5% 925мл')
        self.assertEqual('insufficient', match['status'])  # punctuation differs; no guessing
        self.assertFalse(match['auto_verified'])
        exact = assess('Молоко 2,5% 925 мл', 'Молоко 2,5% 925 мл')
        self.assertEqual('review_required', exact['status'])
        self.assertFalse(exact['auto_verified'])

    def test_percent_and_package_conflicts(self):
        self.assertIn('fat_percent_conflict', assess('Молоко 3,2%', 'Молоко 2,5%')['reasons'])
        self.assertIn('package_conflict', assess('Молоко 900 г', 'Молоко 950 мл')['reasons'])
        self.assertIn('package_conflict', assess('Йогурт 5х80 г', 'Йогурт 400 мл')['reasons'])

    def test_lactose_mismatch_is_conflict(self):
        self.assertEqual('conflict', assess('Молоко безлактозное', 'Молоко')['status'])

    def test_generic_name_cannot_confirm_branded_sku(self):
        result = assess('Молоко Домик в деревне 2,5% 925 мл', 'Молоко')
        self.assertEqual('insufficient', result['status'])
        self.assertEqual(0, result['confidence'])

    def test_read_only_inspection(self):
        db = sqlite3.connect(':memory:')
        db.executescript('CREATE TABLE pb_products(code TEXT, name TEXT);'
                         'CREATE TABLE pb_skus(id INTEGER, retailer TEXT, external_id TEXT, '
                         'title TEXT, match_status TEXT, product_code TEXT);'
                         "INSERT INTO pb_products VALUES('MILK', 'Молоко 2,5% 925 мл');"
                         "INSERT INTO pb_skus VALUES(3,'lenta','77','Молоко 2,5% 925 мл','review_required',NULL);")
        before = db.total_changes
        report = inspect_sku(db, 3)
        self.assertEqual('MILK', report['suggestions'][0]['code'])
        self.assertFalse(report['auto_verified'])
        self.assertEqual(before, db.total_changes)
        self.assertEqual('review_required', db.execute('SELECT match_status FROM pb_skus').fetchone()[0])
