"""Destructive-safety and coverage tests for MEGAPATCH16A inventory."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from catalog_audit16 import DATABASES, _domain, audit_directory, audit_file, main


class CatalogAudit16Tests(unittest.TestCase):
    def test_missing_files_are_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            report = audit_directory(Path(temp))
            self.assertEqual(len(report['files']), len(DATABASES))
            self.assertTrue(all(f['status'] == 'missing' for f in report['files']))

    def test_readonly_and_no_private_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'zhorik.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE pb_skus (id INTEGER, token TEXT)')
                db.execute('INSERT INTO pb_skus VALUES(1, ?)', ('SENSITIVE_MARKER_943',))
                db.execute('CREATE TABLE recipe_items(id INTEGER)')
                db.execute('INSERT INTO recipe_items VALUES(1)')
            previous = path.read_bytes()
            result = audit_file(path)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['domain_totals']['prices'], 1)
            self.assertEqual(result['domain_totals']['recipes'], 1)
            self.assertNotIn('SENSITIVE_MARKER_943', str(result))
            self.assertEqual(path.read_bytes(), previous)

    def test_malformed_database_never_modified(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'zhorik.sqlite3'
            path.write_bytes(b'not a sqlite database')
            result = audit_file(path)
            self.assertIn(result['status'], ('unreadable', 'integrity_error'))
            self.assertEqual(path.read_bytes(), b'not a sqlite database')

    def test_unusual_table_name_not_used_as_query(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'zhorik.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE "bad-table"(id INTEGER)')
            result = audit_file(path)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result.get('skipped_nonstandard_table_names'), 1)

    def test_domain_precedence(self):
        self.assertEqual(_domain('nutrition_products'), 'nutrition')
        self.assertEqual(_domain('recipe_products'), 'recipes')
        self.assertEqual(_domain('shopping_list_items'), 'lists')
        self.assertEqual(_domain('receipt_items'), 'other')
        self.assertEqual(_domain('pb_products'), 'prices')

    def test_cli_fails_without_primary_db(self):
        with tempfile.TemporaryDirectory() as temp:
            from contextlib import redirect_stdout
            from io import StringIO
            with redirect_stdout(StringIO()):
                self.assertEqual(main([temp]), 2)


if __name__ == '__main__':
    unittest.main()
