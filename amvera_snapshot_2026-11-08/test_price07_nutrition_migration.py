"""Backup, migration and rollback simulation on a disposable SQLite only."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from price07_rev2_core import init as price07_init
from price07_nutrition_migration import apply_after_backup


class NutritionMigrationTests(unittest.TestCase):
    def test_additive_schema_after_full_backup_and_restore_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dbpath=root/'original.sqlite3'
            with sqlite3.connect(dbpath) as db:
                price07_init(db)
                db.execute('''INSERT INTO p7_discovered_products
                    (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
                    VALUES('NP1','Товар','hash-np1','lenta','sku-1')''')
                db.commit()
            settings=SimpleNamespace(db_path=str(dbpath),data_dir=str(root))
            with patch('price07_nutrition_migration.settings',settings),\
                 patch('full_db_backup.settings',settings):
                backup=Path(apply_after_backup())
            self.assertTrue(backup.exists())
            with sqlite3.connect(backup) as db:
                self.assertEqual(('ok',),db.execute('PRAGMA integrity_check').fetchone())
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='p7_nutrition_review'").fetchone())
            with sqlite3.connect(dbpath) as db:
                self.assertEqual(1,db.execute('SELECT COUNT(*) FROM p7_discovered_products').fetchone()[0])
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='p7_nutrition_review'").fetchone())
                self.assertEqual(('ok',),db.execute('PRAGMA integrity_check').fetchone())
            restored=root/'restored.sqlite3'
            with sqlite3.connect(backup) as src,sqlite3.connect(restored) as dst:
                src.backup(dst)
            with sqlite3.connect(restored) as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='p7_nutrition_review'").fetchone())
                self.assertEqual(1,db.execute('SELECT COUNT(*) FROM p7_discovered_products').fetchone()[0])

    def test_missing_discovery_schema_aborts_without_modifying_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dbpath=root/'older.sqlite3'
            with sqlite3.connect(dbpath) as db:db.execute('CREATE TABLE user_data(id INTEGER)')
            settings=SimpleNamespace(db_path=str(dbpath),data_dir=str(root))
            with patch('price07_nutrition_migration.settings',settings),\
                 patch('full_db_backup.settings',settings):
                with self.assertRaisesRegex(RuntimeError,'discovered schema absent'):
                    apply_after_backup()
            with sqlite3.connect(dbpath) as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='p7_nutrition_review'").fetchone())


if __name__=='__main__':unittest.main()
