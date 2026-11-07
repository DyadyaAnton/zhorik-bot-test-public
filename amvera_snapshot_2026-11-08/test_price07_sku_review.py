import sqlite3
import unittest

from price_base_v1 import init_schema
from price07_sku_review import init, manual_bind


class ManualSKUReviewTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        init_schema(self.db)
        init(self.db)
        self.db.execute("INSERT INTO pb_products(code,name,category,base_unit) VALUES('M','Молоко 2,5% 925 мл','молочные','л')")
        self.db.execute("INSERT INTO pb_skus(retailer,external_id,title) VALUES('lenta','77','Молоко 2,5% 925 мл')")
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def evidence(self, **changes):
        base = dict(confirmed_title='Молоко 2,5% 925 мл',confirmed_code='M',
                    brand_checked=True,package_checked=True,composition_checked=True,
                    reference='catalog-label-photo-77',note='Проверены этикетка и точная фасовка')
        base.update(changes)
        return base

    def test_manual_review_is_audited_without_verifying_price(self):
        result = manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,evidence=self.evidence())
        self.assertEqual('manually_verified',result['status'])
        self.assertFalse(result['price_verified'])
        self.assertEqual(('M','manually_verified'),self.db.execute('SELECT product_code,match_status FROM pb_skus').fetchone())
        self.assertEqual(1,self.db.execute('SELECT COUNT(*) FROM p7_sku_review_audit').fetchone()[0])

    def test_missing_evidence_is_atomic(self):
        with self.assertRaises(ValueError):
            manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,evidence=self.evidence(reference=''))
        self.assertEqual(('review_required',None),self.db.execute('SELECT match_status,product_code FROM pb_skus').fetchone())
        self.assertEqual(0,self.db.execute('SELECT COUNT(*) FROM p7_sku_review_audit').fetchone()[0])

    def test_conflicting_fat_content_fails_closed(self):
        self.db.execute("UPDATE pb_skus SET title='Молоко 3,2% 925 мл' WHERE id=1")
        with self.assertRaisesRegex(ValueError,'not proven'):
            manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,
                        evidence=self.evidence(confirmed_title='Молоко 3,2% 925 мл'))
        self.assertEqual(0,self.db.execute('SELECT COUNT(*) FROM p7_sku_review_audit').fetchone()[0])

    def test_double_approval_cannot_overwrite(self):
        manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,evidence=self.evidence())
        with self.assertRaisesRegex(ValueError,'already decided'):
            manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,evidence=self.evidence())
        self.assertEqual(1,self.db.execute('SELECT COUNT(*) FROM p7_sku_review_audit').fetchone()[0])

    def test_unrelated_product_cannot_be_verified_even_with_checked_evidence(self):
        self.db.execute("UPDATE pb_skus SET title='Хлеб пшеничный' WHERE id=1")
        with self.assertRaisesRegex(ValueError,'not proven'):
            manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,
                        evidence=self.evidence(confirmed_title='Хлеб пшеничный'))
        self.assertEqual(('review_required',None),self.db.execute('SELECT match_status,product_code FROM pb_skus').fetchone())
        self.assertEqual(0,self.db.execute('SELECT COUNT(*) FROM p7_sku_review_audit').fetchone()[0])

    def test_canonical_unit_mismatch_is_rejected(self):
        self.db.execute("UPDATE pb_products SET base_unit='кг' WHERE code='M'")
        with self.assertRaisesRegex(ValueError,'package unit'):
            manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,evidence=self.evidence())
        self.assertEqual(('review_required',None),self.db.execute('SELECT match_status,product_code FROM pb_skus').fetchone())

    def test_ambiguous_multiunit_package_is_rejected(self):
        self.db.execute("UPDATE pb_products SET name='Набор 2 шт + 500 мл' WHERE code='M'")
        self.db.execute("UPDATE pb_skus SET title='Набор 2 шт + 500 мл' WHERE id=1")
        with self.assertRaisesRegex(ValueError,'package unit'):
            manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,
                        evidence=self.evidence(confirmed_title='Набор 2 шт + 500 мл'))

    def test_unknown_package_does_not_get_verified(self):
        self.db.execute("UPDATE pb_products SET name='Молоко' WHERE code='M'")
        self.db.execute("UPDATE pb_skus SET title='Молоко' WHERE id=1")
        with self.assertRaisesRegex(ValueError,'package unit'):
            manual_bind(self.db,sku_id=1,canonical_code='M',actor_id=123,
                        evidence=self.evidence(confirmed_title='Молоко'))

    def test_schema_init_does_not_commit_external_transaction(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE user_data(value TEXT)')
        db.execute("INSERT INTO user_data VALUES('uncommitted')")
        self.assertTrue(db.in_transaction)
        with self.assertRaisesRegex(RuntimeError,'open transaction'):
            init(db)
        db.rollback()
        self.assertEqual(0,db.execute('SELECT COUNT(*) FROM user_data').fetchone()[0])
        db.close()
