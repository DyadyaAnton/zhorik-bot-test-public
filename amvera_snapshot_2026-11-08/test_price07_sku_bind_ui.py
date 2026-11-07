import time
import unittest
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

def callback(user_id=123, chat_id=123, token='test-token'):
    chat=SimpleNamespace(id=chat_id,type='private')
    message=SimpleNamespace(chat=chat,answer=AsyncMock())
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id),message=message,
                           data='p7:bind:'+token,answer=AsyncMock())


class BindUIUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Defer import until legacy Q1 tests have installed their aiogram stubs.
        import price07_ui
        self.ui = price07_ui

    def tearDown(self):
        self.ui._pending_sku_reviews.clear()

    def pending(self):
        self.ui._pending_sku_reviews['test-token']={
            'owner_id':123,'chat_id':123,'created_at':time.monotonic(),
            'sku_id':1,'code':'M','sku_title':'Молоко 925 мл',
            'canonical_title':'Молоко 925 мл','reference':'photo-77-source'}

    async def test_unauthorized_callback_never_consumes_token(self):
        self.pending()
        cb=callback(user_id=999)
        with patch.object(self.ui,'settings',SimpleNamespace(sandbox_owner_id=123)):
            await self.ui.price07_sku_bind_confirm(cb)
        self.assertIn('test-token',self.ui._pending_sku_reviews)
        cb.answer.assert_awaited_once()

    async def test_expired_callback_does_not_write(self):
        self.pending()
        self.ui._pending_sku_reviews['test-token']['created_at']-=601
        cb=callback()
        with patch.object(self.ui,'settings',SimpleNamespace(sandbox_owner_id=123)),patch.object(self.ui,'_sku_review_commit') as writer:
            await self.ui.price07_sku_bind_confirm(cb)
        writer.assert_not_called()
        self.assertNotIn('test-token',self.ui._pending_sku_reviews)

    async def test_replay_is_rejected_after_single_use(self):
        self.pending()
        cb=callback()
        with patch.object(self.ui,'settings',SimpleNamespace(sandbox_owner_id=123)),patch.object(self.ui,'_sku_review_commit',return_value={'status':'manually_verified'}) as writer:
            await self.ui.price07_sku_bind_confirm(cb)
            await self.ui.price07_sku_bind_confirm(cb)
        writer.assert_called_once()
        self.assertNotIn('test-token',self.ui._pending_sku_reviews)
        self.assertEqual(2,cb.answer.await_count)

    async def test_wrong_private_chat_does_not_consume_token(self):
        self.pending()
        cb=callback(chat_id=456)
        with patch.object(self.ui,'settings',SimpleNamespace(sandbox_owner_id=123)):
            await self.ui.price07_sku_bind_confirm(cb)
        self.assertIn('test-token',self.ui._pending_sku_reviews)

    def test_evidence_link_rejects_unsupported_or_unsafe_sources(self):
        accepted = ('https://example.org/photo/77', 'https://example.org:443/a')
        rejected = ('photo-77-source','http://example.org/label','https://example.org:444/a',
                    'https://user:secret@example.org/label','https://example.org/space here',
                    'https://example.org\nmalicious', 'https://', 'file:///photo.jpg')
        for link in accepted:
            with self.subTest(link=link):
                self.assertTrue(self.ui._valid_evidence_url(link))
        for link in rejected:
            with self.subTest(link=link):
                self.assertFalse(self.ui._valid_evidence_url(link))

    async def test_full_prepare_confirm_writes_sqlite_and_audit(self):
        from price_base_v1 import init_schema
        from price07_sku_review import init
        with tempfile.TemporaryDirectory() as temp:
            dbpath=Path(temp)/'test.sqlite3'
            with sqlite3.connect(dbpath) as db:
                init_schema(db)
                init(db)
                db.execute("INSERT INTO pb_products(code,name,category,base_unit) VALUES('M','Молоко 925 мл','молоко','л')")
                db.execute("INSERT INTO pb_skus(retailer,external_id,title) VALUES('lenta','77','Молоко 925 мл')")
            message=SimpleNamespace(from_user=SimpleNamespace(id=123),
                                    chat=SimpleNamespace(id=123,type='private'),
                                    text='/price07_sku_bind 1 M https://example.org/photo/77',answer=AsyncMock())
            with patch.object(self.ui,'settings',SimpleNamespace(sandbox_owner_id=123)),\
                 patch.object(self.ui,'connect_db',side_effect=lambda: sqlite3.connect(dbpath)):
                await self.ui.price07_sku_bind_prepare(message)
                self.assertEqual(1,len(self.ui._pending_sku_reviews))
                token=next(iter(self.ui._pending_sku_reviews))
                cb=callback(token=token)
                await self.ui.price07_sku_bind_confirm(cb)
            with sqlite3.connect(dbpath) as db:
                self.assertEqual(('M','manually_verified'),db.execute('SELECT product_code,match_status FROM pb_skus').fetchone())
                self.assertEqual(1,db.execute('SELECT COUNT(*) FROM p7_sku_review_audit').fetchone()[0])

    async def test_prepare_with_missing_migration_does_not_issue_confirmation(self):
        from price_base_v1 import init_schema
        with tempfile.TemporaryDirectory() as temp:
            dbpath=Path(temp)/'pre_migration.sqlite3'
            with sqlite3.connect(dbpath) as db:
                init_schema(db)
                db.execute("INSERT INTO pb_products(code,name,category,base_unit) VALUES('M','Молоко 925 мл','молоко','л')")
                db.execute("INSERT INTO pb_skus(retailer,external_id,title) VALUES('lenta','77','Молоко 925 мл')")
            message=SimpleNamespace(from_user=SimpleNamespace(id=123),
                                    chat=SimpleNamespace(id=123,type='private'),
                                    text='/price07_sku_bind 1 M https://example.org/photo/77',answer=AsyncMock())
            with patch.object(self.ui,'settings',SimpleNamespace(sandbox_owner_id=123)),\
                 patch.object(self.ui,'connect_db',side_effect=lambda: sqlite3.connect(dbpath)):
                await self.ui.price07_sku_bind_prepare(message)
            self.assertEqual({},self.ui._pending_sku_reviews)
