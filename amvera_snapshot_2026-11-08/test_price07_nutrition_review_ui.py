"""Owner-only Telegram preview and single-use confirmation for KBJU evidence."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from price07_rev2_core import init
from price07_nutrition_review import init as review_init
import price07_ui


class NutritionReviewUITests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        price07_ui._pending_nutrition_reviews.clear()

    async def test_owner_preview_confirm_single_use_and_other_user_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'nutrition.sqlite3'
            with sqlite3.connect(path) as db:
                init(db)
                db.execute('''INSERT INTO p7_discovered_products
                    (code,canonical_name,title_fingerprint,first_retailer,first_sku_id)
                    VALUES('NPTEST','Тестовая груша','f-123','lenta','sku-test')''')
                db.commit()
                review_init(db)
            def connect():return sqlite3.connect(path)
            chat=SimpleNamespace(id=71,type='private')
            owner=SimpleNamespace(from_user=SimpleNamespace(id=71),chat=chat,
                text='/price07_nutrition_verify NPTEST 57 0.4 0.1 15.2 - https://example.org/label',
                answer=AsyncMock())
            other=SimpleNamespace(from_user=SimpleNamespace(id=72),chat=chat,
                text=owner.text,answer=AsyncMock())
            with patch.object(price07_ui,'settings',SimpleNamespace(sandbox_owner_id=71)),\
                 patch.object(price07_ui,'connect_db',side_effect=connect):
                await price07_ui.price07_nutrition_verify_prepare(other)
                other.answer.assert_not_called()
                await price07_ui.price07_nutrition_verify_prepare(owner)
                self.assertEqual(1,len(price07_ui._pending_nutrition_reviews))
                token=next(iter(price07_ui._pending_nutrition_reviews))
                callback=SimpleNamespace(from_user=SimpleNamespace(id=71),
                    message=SimpleNamespace(chat=chat,answer=AsyncMock()),
                    data='p7:nutrition:'+token,answer=AsyncMock())
                await price07_ui.price07_nutrition_verify_confirm(callback)
                callback.message.answer.assert_awaited_once()
                await price07_ui.price07_nutrition_verify_confirm(callback)
                self.assertEqual(2,callback.answer.await_count)
            with sqlite3.connect(path) as db:
                self.assertEqual(1,db.execute('SELECT COUNT(*) FROM p7_nutrition_review').fetchone()[0])
                self.assertEqual(1,db.execute('SELECT COUNT(*) FROM p7_nutrition_review_audit').fetchone()[0])


if __name__=='__main__':unittest.main()
