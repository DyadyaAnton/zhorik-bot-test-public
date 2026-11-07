import sqlite3
import tempfile
import unittest
from pathlib import Path

from price_base_v1 import init_schema
from price07_weekly import run, latest, week_key


class Price07Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        with self.db() as db:
            init_schema(db)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.execute('PRAGMA foreign_keys=ON')
        return db

    @staticmethod
    def fetch(path, params):
        assert path == '/catalog/v1/items'
        return {'items': [{'id': 'SKU-77', 'name': 'Гречка ЛЕНТА 5х80г',
                           'prices': {'price': 4999}}]}

    def test_weekly_idempotence_unverified_and_unknown_region(self):
        out = run(self.db, queries=('гречка',), store_id=4527, fetch=self.fetch)
        self.assertEqual(out['status'], 'completed')
        self.assertEqual(out['new_observations'], 1)
        again = run(self.db, queries=('гречка',), store_id=4527, fetch=self.fetch)
        self.assertEqual(again['status'], 'already_run')
        with self.db() as db:
            self.assertEqual(db.execute('select count(*) from pb_observations').fetchone()[0], 1)
            self.assertEqual(db.execute('select review_status from pb_observations').fetchone()[0], 'unverified')
            self.assertEqual(db.execute('select region_confirmed from pb_observations').fetchone()[0], 0)
            self.assertIsNone(db.execute('select product_code from pb_skus').fetchone()[0])
            self.assertEqual(latest(db)['status'], 'completed')

    def test_failure_recorded_and_does_not_create_prices(self):
        def fail(*args):
            raise RuntimeError('source unavailable')
        report = run(self.db, queries=('молоко',), fetch=fail)
        self.assertEqual(report['status'], 'failed')
        with self.db() as db:
            self.assertEqual(db.execute('select count(*) from pb_observations').fetchone()[0], 0)
            self.assertEqual(latest(db)['failed'], 1)

    def test_failed_job_manual_retry_without_duplicate_weekly_success(self):
        def fail(*args):
            raise RuntimeError('offline')
        self.assertEqual(run(self.db, queries=('молоко',), fetch=fail)['status'], 'failed')
        completed = run(self.db, trigger='manual', queries=('молоко',), fetch=self.fetch)
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(run(self.db, trigger='manual', queries=('молоко',), fetch=self.fetch)['status'], 'already_run')

    def test_empty_response_cannot_be_completed(self):
        out = run(self.db, queries=('гречка',), fetch=lambda *_: {'items': []})
        self.assertEqual(out['status'], 'failed')
        self.assertEqual(out['empty'], 1)
        self.assertEqual(out['new_observations'], 0)

    def test_no_automatic_schema_creation(self):
        self.path.unlink()
        with self.assertRaises(RuntimeError):
            run(self.db, queries=('молоко',), fetch=self.fetch)
        with self.db() as db:
            self.assertNotIn('pb_skus', {row[0] for row in db.execute("SELECT name FROM sqlite_master")})

    def test_invalid_queries_rejected_before_db_change(self):
        for queries in ((), tuple('a' for _ in range(31)), ('',)):
            with self.assertRaises(ValueError):
                run(self.db, queries=queries, fetch=self.fetch)
        with self.db() as db:
            self.assertEqual(db.execute('select count(*) from pb_refresh_runs').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
