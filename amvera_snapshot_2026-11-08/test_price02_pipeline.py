"""Offline PRICE 02 tests. All retailer data are synthetic; no network calls."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from price02_pipeline import ensure, stage, _identity, rollback_if_open, import_legacy_candidates, stage_public_candidates
from planning_prices import prices_for_menu, verified_market_prices


class Price02Tests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        ensure(self.db)

    def tearDown(self):
        self.db.close()

    def sample(self, **extra):
        row = {'product_id':'vv123','name':'Молоко 2,5%', 'price_rub':115,
               'base_unit':'л','price_per_base_unit':115,
               'match_status':'likely_match','observed_at':'2026-11-05T12:00:00+00:00'}
        row.update(extra)
        return row

    def test_other_retailer_candidates_remain_unverified_and_idempotent(self):
        stamp = datetime.now(timezone.utc).isoformat()
        rows = [{'product_id':'l-1', 'title':'Молоко 1 л', 'price_rub':105,
                 'store_id':'unknown-test', 'observed_at':stamp}]
        for retailer in ('lenta','auchan','pyaterochka','perekrestok','dixy','yarche'):
            result = stage_public_candidates(self.db, retailer, rows, source=f'{retailer}_public')
            self.assertEqual(result['inserted_observations'], 1)
            self.assertEqual(stage_public_candidates(self.db, retailer, rows, source=f'{retailer}_public')['duplicates'], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM pb_skus').fetchone()[0], 6)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pb_observations WHERE review_status='unverified' AND region_key='unknown'").fetchone()[0], 6)

    def test_other_retailer_batch_rejects_bad_data_and_overflow(self):
        stamp = datetime.now(timezone.utc).isoformat()
        rows = [{'product_id':'x', 'title':'Test', 'price_rub':30, 'observed_at':stamp},
                {'product_id':'y', 'title':'Test', 'price_rub':30, 'observed_at':'2099-01-01T12:00:00Z'},
                {'product_id':'z', 'title':'Test', 'price_rub':-30, 'observed_at':stamp}]
        self.assertEqual(stage_public_candidates(self.db,'lenta',rows,source='lenta_probe')['skipped'], 2)
        with self.assertRaises(ValueError):
            stage_public_candidates(self.db,'lenta',rows,source='lenta_probe',limit=2)
        with self.assertRaises(ValueError):
            stage_public_candidates(self.db,'unknown',[],source='lenta_probe')

    def test_unknown_region_never_auto_verifies(self):
        report = stage(self.db,'vkusvill','Молоко 2,5%', [self.sample()])
        self.assertEqual(report['verified'],0)
        self.assertEqual(report['review_required'],1)
        self.assertEqual(self.db.execute('SELECT match_status FROM pb_skus').fetchone()[0], 'review_required')

    def test_explicit_region_exact_product_only_verifies_identity_not_price_type(self):
        report = stage(self.db,'vkusvill','Молоко 2,5%', [self.sample()],
                       region='Москва',region_confirmed=True)
        self.assertEqual(report['verified'],0)
        row=self.db.execute('SELECT region,region_key,review_status FROM pb_observations').fetchone()
        self.assertEqual(row,('Москва','Москва','unverified'))
        self.assertEqual(self.db.execute('SELECT match_status FROM pb_skus').fetchone()[0], 'auto_verified')
        self.assertEqual(self.db.execute('SELECT normal_price_rub,promo_price_rub,current_price_rub FROM pb_observations').fetchone(),
                         (None,None,115.0))

    def test_brand_variants_and_wrong_unit_remain_unverified(self):
        report = stage(self.db,'vkusvill','Молоко 2,5%', [
            self.sample(name='Молоко 2,5% Эко 1 л', product_id='x1'),
            self.sample(base_unit='кг',product_id='x2')
        ],region='Москва',region_confirmed=True)
        self.assertEqual(report['verified'],0)
        self.assertEqual(report['review_required'],2)

    def test_duplicate_input_does_not_duplicate_observation(self):
        stage(self.db,'vkusvill','Молоко 2,5%',[self.sample()])
        stage(self.db,'vkusvill','Молоко 2,5%',[self.sample()])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM pb_observations').fetchone()[0],1)

    def test_unsupported_store_rejected(self):
        with self.assertRaises(ValueError):
            stage(self.db,'lenta','молоко',[])

    def test_no_products_in_price_menu_legacy_tables(self):
        self.db.execute('CREATE TABLE prices(id INTEGER PRIMARY KEY, price REAL)')
        self.db.execute('INSERT INTO prices(price) VALUES (555)')
        stage(self.db,'vkusvill','Молоко 2,5%',[self.sample()])
        self.assertEqual(self.db.execute('SELECT price FROM prices').fetchone()[0],555)

    def test_stage_is_atomic_when_observation_insert_fails(self):
        self.db.execute('''CREATE TRIGGER reject_stage BEFORE INSERT ON pb_observations
                           BEGIN SELECT RAISE(ABORT,'failure'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            stage(self.db,'vkusvill','Молоко 2,5%',[self.sample()])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM pb_skus').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM pb_observations').fetchone()[0],0)

    def test_rollback_helper_only_rolls_back_active_transaction(self):
        self.db.commit()  # setUp seeded the catalog in a default SQLite transaction
        self.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.db.in_transaction)
        rollback_if_open(self.db)
        self.assertFalse(self.db.in_transaction)
        rollback_if_open(self.db)  # idempotent when no transaction exists

    def test_locked_begin_does_not_trigger_secondary_rollback_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'locked.sqlite3'
            first = sqlite3.connect(file, timeout=0.05, isolation_level=None)
            second = sqlite3.connect(file, timeout=0.05, isolation_level=None)
            try:
                first.execute('CREATE TABLE sentinel (value INTEGER)')
                first.execute('BEGIN IMMEDIATE')
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    second.execute('BEGIN IMMEDIATE')
                self.assertFalse(second.in_transaction)
                rollback_if_open(second)
                self.assertFalse(second.in_transaction)
            finally:
                first.rollback()
                first.close()
                second.close()

    def _verified_quote(self, *, region='Москва', age_days=0, price=117):
        observed = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
        stage(self.db, 'vkusvill', 'Молоко 2,5%', [self.sample(
            observed_at=observed, price_rub=price, price_per_base_unit=price)],
            region=region, region_confirmed=True)
        sku = self.db.execute('SELECT id FROM pb_skus').fetchone()[0]
        self.db.execute("UPDATE pb_skus SET match_status='manually_verified' WHERE id=?", (sku,))
        self.db.execute("UPDATE pb_observations SET review_status='manually_verified' WHERE sku_id=?", (sku,))

    def test_menu_price_requires_verified_quote_and_region(self):
        stage(self.db, 'vkusvill', 'Молоко 2,5%', [self.sample()],
              region='Москва', region_confirmed=True)
        self.assertEqual(verified_market_prices(self.db, city='Москва'), {})
        self.assertEqual(verified_market_prices(self.db), {})

    def test_verified_quote_used_in_menu_only_for_matching_city(self):
        self._verified_quote()
        prices, estimates = prices_for_menu(self.db, city='Москва',
                                            selected_shops=['ВкусВилл'])
        self.assertEqual(prices[('молоко 2,5%', 'л')], 117)
        self.assertEqual(prices[('молоко', 'л')], 117)
        self.assertNotIn(('молоко', 'л'), estimates)
        other, other_estimates = prices_for_menu(self.db, city='Казань', selected_shops=['ВкусВилл'])
        self.assertIn(('молоко', 'л'), other_estimates)
        self.assertNotEqual(other[('молоко', 'л')], 117)

    def test_verified_menu_price_exposes_source_and_region(self):
        self._verified_quote()
        values, estimates, provenance = prices_for_menu(
            self.db, city='Москва', selected_shops=['ВкусВилл'], include_provenance=True)
        self.assertEqual(117, values[('молоко', 'л')])
        self.assertNotIn(('молоко', 'л'), estimates)
        item=provenance[('молоко', 'л')]
        self.assertEqual('verified_market',item['quality'])
        self.assertEqual('vkusvill',item['retailer'])
        self.assertEqual('Москва',item['region'])
        self.assertTrue(item['observed_at'])

    def test_stale_and_unselected_store_cannot_change_menu(self):
        self._verified_quote(age_days=20)
        self.assertEqual(verified_market_prices(self.db, city='Москва', selected_shops=['ВкусВилл']), {})
        self.assertEqual(verified_market_prices(self.db, city='Москва',
                         selected_shops=['Пятёрочка']), {})

    def test_no_selected_store_is_not_unrestricted_market_access(self):
        self._verified_quote()
        self.assertEqual(verified_market_prices(self.db, city='Москва'), {})
        prices, estimates = prices_for_menu(self.db, city='Москва')
        self.assertIn(('молоко', 'л'), estimates)

    def test_future_dated_quote_is_not_current(self):
        self._verified_quote(age_days=-1)
        self.assertEqual(verified_market_prices(self.db, city='Москва',
                                                selected_shops=['ВкусВилл']), {})

    def test_unknown_region_and_unverified_never_enter_menu(self):
        stage(self.db, 'vkusvill', 'Молоко 2,5%', [self.sample()])
        price_map, estimates = prices_for_menu(self.db, city='Москва')
        self.assertIn(('молоко', 'л'), estimates)
        # A planning assumption may coincidentally equal the observed price;
        # provenance (estimate_keys), not numeric inequality, is the contract.
        self.assertEqual(price_map[('молоко', 'л')], 115)

    def test_legacy_backfill_dry_run_and_idempotent_unverified(self):
        self.db.execute('''CREATE TABLE zb_candidates(
            id INTEGER PRIMARY KEY,retailer TEXT,product_id TEXT,title TEXT,
            price_rub TEXT,unit_price_rub TEXT,unit_basis TEXT,observed_at TEXT)''')
        self.db.executemany('''INSERT INTO zb_candidates VALUES(?,?,?,?,?,?,?,?)''', [
            (1,'vkusvill','vv-1','Молоко 2,5%','115','115','л','2026-11-05T12:00:00Z'),
            (2,'magnit','m-1','Гречка 900 г','99','110','кг','2026-11-05T12:00:00Z'),
            (3,'vkusvill','bad','Пустая цена',None,'100','кг','2026-11-05T12:00:00Z')])
        self.assertEqual(import_legacy_candidates(self.db)['valid'],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM pb_observations').fetchone()[0],0)
        report = import_legacy_candidates(self.db,dry_run=False)
        self.assertEqual((report['inserted_skus'], report['inserted_observations']),(2,2))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pb_observations WHERE review_status='unverified' AND region_confirmed=0").fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM zb_candidates').fetchone()[0],3)
        self.assertEqual(set(x[0] for x in self.db.execute('SELECT source FROM pb_observations')),
                         {'legacy_zb_candidates:1', 'legacy_zb_candidates:2'})
        retry = import_legacy_candidates(self.db,dry_run=False)
        self.assertEqual((retry['inserted_skus'], retry['inserted_observations']),(0,0))
        self.assertEqual(verified_market_prices(self.db,city='Москва',selected_shops=['ВкусВилл']),{})

    def test_legacy_backfill_rollback_all_if_write_fails(self):
        self.db.execute('''CREATE TABLE zb_candidates(id INTEGER PRIMARY KEY,retailer TEXT,
            product_id TEXT,title TEXT,price_rub TEXT,unit_price_rub TEXT,
            unit_basis TEXT,observed_at TEXT)''')
        self.db.execute("INSERT INTO zb_candidates VALUES(1,'vkusvill','x','Молоко','115','115','л','2026-11-05T12:00:00Z')")
        self.db.execute('''CREATE TRIGGER reject_backfill BEFORE INSERT ON pb_observations
                            BEGIN SELECT RAISE(ABORT,'reject'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            import_legacy_candidates(self.db,dry_run=False)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pb_skus').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pb_observations').fetchone()[0],0)

    def test_legacy_external_ids_cannot_collide_by_truncation(self):
        self.db.execute('''CREATE TABLE zb_candidates(id INTEGER PRIMARY KEY,retailer TEXT,
            product_id TEXT,title TEXT,price_rub TEXT,unit_price_rub TEXT,
            unit_basis TEXT,observed_at TEXT)''')
        prefix = 'x' * 120
        self.db.executemany('INSERT INTO zb_candidates VALUES(?,?,?,?,?,?,?,?)', [
            (1,'magnit',prefix+'1','Product A','100','100','кг','2026-11-05'),
            (2,'magnit',prefix+'2','Product B','200','200','кг','2026-11-05')])
        report = import_legacy_candidates(self.db,dry_run=False)
        self.assertEqual((report['skipped'],report['inserted_skus'],
                          report['inserted_observations']),(2,0,0))

    def test_legacy_import_bound_fails_closed_not_partial(self):
        self.db.execute('''CREATE TABLE zb_candidates(id INTEGER PRIMARY KEY,retailer TEXT,
            product_id TEXT,title TEXT,price_rub TEXT,unit_price_rub TEXT,
            unit_basis TEXT,observed_at TEXT)''')
        self.db.executemany('INSERT INTO zb_candidates VALUES(?,?,?,?,?,?,?,?)', [
            (i,'magnit',str(i),'Product','100','100','кг','2026-11-05')
            for i in range(1,4)])
        preview = import_legacy_candidates(self.db,limit=2)
        self.assertTrue(preview['truncated'])
        self.assertEqual(preview['total_available'],3)
        with self.assertRaises(ValueError):
            import_legacy_candidates(self.db,limit=2,dry_run=False)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pb_observations').fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()

