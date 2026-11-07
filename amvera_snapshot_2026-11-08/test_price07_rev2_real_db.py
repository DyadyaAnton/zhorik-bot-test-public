"""Run with JORIK_TEST_DB_COPY=/path/to/offline.sqlite3 (never live /data)."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from price07_rev2_core import init, claim
from price07_unified_catalog import catalog_snapshot
from price07_review_migration import apply_after_backup


@unittest.skipUnless(os.getenv('JORIK_TEST_DB_COPY'), 'offline DB snapshot not supplied')
class RealDatabaseIsolationTests(unittest.TestCase):
    def test_review_schema_backup_and_restore_on_real_snapshot_clone(self):
        original = Path(os.environ['JORIK_TEST_DB_COPY']).resolve()
        self.assertTrue(original.is_file())
        self.assertNotIn('/data/', str(original))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'migration_trial.sqlite3'
            with sqlite3.connect(f'file:{original}?mode=ro', uri=True) as source:
                with sqlite3.connect(target) as copied:
                    source.backup(copied)
            with sqlite3.connect(target) as db:
                before = {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                          for name in ('user_prices','pb_products','pb_skus','pb_observations')}
            fake = SimpleNamespace(db_path=str(target),data_dir=str(Path(tmp)))
            with patch('price07_review_migration.settings',fake),patch('full_db_backup.settings',fake):
                backed_up = Path(apply_after_backup())
            self.assertTrue(backed_up.is_file())
            with sqlite3.connect(target) as upgraded:
                self.assertIsNotNone(upgraded.execute("SELECT name FROM sqlite_master WHERE name='p7_sku_review_audit'").fetchone())
                self.assertEqual(before,{name: upgraded.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                                         for name in before})
                self.assertEqual(('ok',),upgraded.execute('PRAGMA integrity_check').fetchone())
            # Rehearse recovery into a NEW target; never overwrite the source.
            restored = Path(tmp) / 'restored.sqlite3'
            with sqlite3.connect(f'file:{backed_up}?mode=ro', uri=True) as backup:
                with sqlite3.connect(restored) as output:
                    backup.backup(output)
            with sqlite3.connect(restored) as recovered:
                self.assertIsNone(recovered.execute("SELECT name FROM sqlite_master WHERE name='p7_sku_review_audit'").fetchone())
                self.assertEqual(before,{name: recovered.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                                         for name in before})
                self.assertEqual(('ok',),recovered.execute('PRAGMA integrity_check').fetchone())

    def test_schema_and_unified_view_do_not_change_legacy_data(self):
        original = Path(os.environ['JORIK_TEST_DB_COPY']).resolve()
        self.assertTrue(original.is_file())
        self.assertNotIn('/data/', str(original))
        with tempfile.TemporaryDirectory() as tmp:
            cloned = Path(tmp) / 'private_test_copy.sqlite3'
            with sqlite3.connect(f'file:{original}?mode=ro', uri=True) as src:
                with sqlite3.connect(cloned) as dst:
                    src.backup(dst)
            with sqlite3.connect(cloned, isolation_level=None) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                preserved = [name for name in ('user_prices','pb_products','pb_skus','pb_observations',
                                               'zr_recipes','zm_menu_history','lists') if name in tables]
                def counts():
                    return {name: db.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                            for name in preserved}
                before = counts()
                init(db)
                snap = catalog_snapshot(db, 1)
                self.assertGreater(snap['products_total'], 0)
                self.assertIsNotNone(claim(db, 'lenta'))
                self.assertEqual(before, counts())
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())


if __name__ == '__main__':
    unittest.main()
