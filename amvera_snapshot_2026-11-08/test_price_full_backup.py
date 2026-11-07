"""PRICE03 full recovery snapshot tests: disposable databases only."""
import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from price_backup import create_full_snapshot


class FullSnapshotTests(unittest.TestCase):
    def test_full_snapshot_preserves_unrelated_data_and_price_schema(self):
        with tempfile.TemporaryDirectory() as root:
            dbfile = Path(root) / 'live.sqlite3'
            with sqlite3.connect(dbfile) as db:
                db.execute('CREATE TABLE private_lists (id INTEGER PRIMARY KEY, title TEXT)')
                db.execute("INSERT INTO private_lists(title) VALUES ('До зарплаты')")
                db.execute('CREATE TABLE pb_skus (id INTEGER PRIMARY KEY, retailer TEXT)')
                db.execute("INSERT INTO pb_skus(retailer) VALUES ('vkusvill')")
            with patch('price_backup.connect_db', side_effect=lambda: sqlite3.connect(dbfile)):
                result = create_full_snapshot(destination_dir=Path(root) / 'backups')
            self.assertTrue(result.is_file())
            recovered = Path(root) / 'recovered.sqlite3'
            with gzip.open(result, 'rb') as src:
                recovered.write_bytes(src.read())
            with sqlite3.connect(recovered) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('SELECT title FROM private_lists').fetchone()[0], 'До зарплаты')
                self.assertEqual(db.execute('SELECT retailer FROM pb_skus').fetchone()[0], 'vkusvill')


if __name__ == '__main__':
    unittest.main()
