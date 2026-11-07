import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from price_base_v1 import init_schema
from price07_rev2_core import init, claim, complete, query_catalog, eligible_for_menu, status


class Price07Rev2CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'price.sqlite3'
        with self.db() as db:
            init_schema(db)
            db.executemany('INSERT INTO pb_products(code,name,category,base_unit) VALUES(?,?,?,?)',
                           [('CP0001','Молоко 2,5%','Молочные','л'),
                            ('CP0002','Гречка','Крупы','кг')])
            init(db)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self):
        return sqlite3.connect(self.path, isolation_level=None, timeout=5)

    def claim_one(self, source='lenta'):
        with self.db() as db:
            return claim(db, source)

    def complete_one(self, task, *, outcome='success', rows=()):
        with self.db() as db:
            return complete(db, task, outcome=outcome, rows=rows)

    def test_atomic_claim_does_not_double_assign(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.claim_one) for _ in range(2)]
            tasks = [f.result() for f in futures]
        self.assertEqual(sum(bool(task) for task in tasks), 1)

    def test_discovery_never_eligible_without_both_checks(self):
        task = self.claim_one()
        rows = [{'product_id':'l-1','title':'Молоко бренд 2,5%, 950мл',
                 'price_rub':99.99,'store_id':4515,
                 'observed_at':datetime.now(timezone.utc).isoformat()}]
        report = self.complete_one(task, rows=rows)
        self.assertEqual(report['observations'], 1)
        self.assertEqual(report['discovered'], 1)
        with self.db() as db:
            code = db.execute('SELECT code FROM p7_discovered_products').fetchone()[0]
            self.assertFalse(eligible_for_menu(db, code))
            self.assertEqual(db.execute('SELECT review_status FROM pb_observations').fetchone()[0], 'unverified')
            self.assertEqual(db.execute('SELECT region_confirmed FROM pb_observations').fetchone()[0], 0)
            self.assertEqual(query_catalog(db, 'NP', 10), [])
            db.execute('''UPDATE p7_discovered_products SET kbju_verified=1,
                allergens_verified=1,review_status='verified' WHERE code=?''',(code,))
            # Bare flags are insufficient without independent audited KBJU.
            self.assertFalse(eligible_for_menu(db, code))
            self.assertEqual(query_catalog(db, 'NP', 10), [])

    def test_stale_lease_cannot_settle_twice(self):
        task = self.claim_one()
        result = self.complete_one(task, outcome='empty')
        self.assertFalse(result['stale'])
        self.assertTrue(self.complete_one(task, outcome='empty')['stale'])
        next_task = self.claim_one()
        self.assertEqual(next_task['code'], 'CP0002')

    def test_network_failure_isolated_and_logged(self):
        task = self.claim_one('magnit')
        self.complete_one(task, outcome='error')
        self.assertIsNone(self.claim_one('magnit'))  # backoff, no 403 loop
        with self.db() as db:
            retried = claim(db, 'magnit', now=datetime.now(timezone.utc)+timedelta(hours=7))
        self.assertEqual(retried['code'], task['code'])
        with self.db() as db:
            row = next(x for x in status(db) if x['source']=='magnit')
            self.assertEqual(row['errors'], 1)
            self.assertEqual(row['processed'], 1)
            self.assertIsNotNone(row['retry_after'])

    def test_missing_schema_fails_closed(self):
        with self.db() as db:
            db.execute('DROP TABLE p7_source_progress')
        with self.assertRaises(sqlite3.OperationalError):
            self.claim_one()

    def test_first_boot_initializes_price02_dependencies(self):
        with sqlite3.connect(':memory:', isolation_level=None) as db:
            init(db)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM pb_products').fetchone()[0], 0)
            self.assertIsNotNone(claim(db, 'lenta'))


if __name__ == '__main__':
    unittest.main()
