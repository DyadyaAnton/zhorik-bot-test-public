import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from price_base_v1 import init_schema
from price07_review_migration import apply_after_backup


class BackupGatedReviewMigrationTests(unittest.TestCase):
    def test_creates_verified_backup_before_additive_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dbpath = root / 'realistic.sqlite3'
            with sqlite3.connect(dbpath) as db:
                init_schema(db)
                db.execute("INSERT INTO pb_products(code,name,category,base_unit) VALUES('M','Молоко 925 мл','молоко','л')")
                db.execute("INSERT INTO pb_skus(retailer,external_id,title) VALUES('lenta','77','Молоко 925 мл')")
            settings = SimpleNamespace(db_path=str(dbpath),data_dir=str(root))
            with patch('price07_review_migration.settings',settings),patch('full_db_backup.settings',settings):
                backup_path=Path(apply_after_backup())
            self.assertTrue(backup_path.is_file())
            with sqlite3.connect(backup_path) as backup:
                self.assertEqual(('ok',),backup.execute('PRAGMA integrity_check').fetchone())
                self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='p7_sku_review_audit'").fetchone())
                self.assertEqual(1,backup.execute('SELECT COUNT(*) FROM pb_skus').fetchone()[0])
            with sqlite3.connect(dbpath) as db:
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='p7_sku_review_audit'").fetchone())
                self.assertEqual(('review_required',None),db.execute('SELECT match_status,product_code FROM pb_skus').fetchone())

    def test_missing_required_tables_fails_without_migrating(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dbpath=root/'incomplete.sqlite3'
            with sqlite3.connect(dbpath) as db:
                db.execute('CREATE TABLE harmless(id INTEGER)')
            settings=SimpleNamespace(db_path=str(dbpath),data_dir=str(root))
            with patch('price07_review_migration.settings',settings),patch('full_db_backup.settings',settings):
                with self.assertRaisesRegex(RuntimeError,'Missing required table'):
                    apply_after_backup()
            with sqlite3.connect(dbpath) as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='p7_sku_review_audit'").fetchone())
