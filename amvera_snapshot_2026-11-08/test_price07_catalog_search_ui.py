"""Offline tests for private catalog search, paging token, and history UX."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from price07_rev2_core import init
import price_ui


class SearchUIFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_search_and_detail_show_only_own_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'catalog.sqlite3'
            with sqlite3.connect(path) as db:
                init(db)
                db.execute('''CREATE TABLE user_prices(user_id INT,product TEXT,shop TEXT,
                    unit TEXT,price REAL,observed_at TEXT)''')
                db.executemany('INSERT INTO user_prices VALUES(?,?,?,?,?,?)',[
                    (71,'Молоко 2,5%','Мой магазин','л',115,'2026-11-05'),
                    (72,'Молоко 2,5%','Секретный магазин','л',900,'2026-11-05')])
            def connect():
                return sqlite3.connect(path)
            data={}
            state=SimpleNamespace(
                update_data=AsyncMock(side_effect=lambda **kw:data.update(kw)),
                set_state=AsyncMock(),
                get_data=AsyncMock(side_effect=lambda:data.copy()))
            message=SimpleNamespace(text='молоко',from_user=SimpleNamespace(id=71),
                                    answer=AsyncMock())
            cb=SimpleNamespace(data=None,from_user=SimpleNamespace(id=71),
                               message=SimpleNamespace(edit_text=AsyncMock()),
                               answer=AsyncMock())
            sender=AsyncMock()
            with patch.object(price_ui,'connect_db',side_effect=connect),\
                 patch.object(price_ui,'check_message',new_callable=AsyncMock,return_value=True),\
                 patch.object(price_ui,'check_callback',new_callable=AsyncMock,return_value=True),\
                 patch.object(price_ui,'safe_call',sender):
                await price_ui.search_price_catalog(message,state)
                self.assertTrue(data['catalog_search_codes'])
                with connect() as db:
                    from price07_unified_catalog import catalog_snapshot
                    code=next(p['code'] for p in catalog_snapshot(db,71)['products']
                              if p['name']=='Молоко 2,5%')
                index=data['catalog_search_codes'].index(code)
                cb.data=f'price:detail:{index}:{data["catalog_search_token"]}'
                await price_ui.action(cb,state)
                text=sender.call_args.args[1]
                self.assertIn('Мой магазин',text)
                self.assertNotIn('Секретный магазин',text)
                self.assertIn('не гарантия актуальной цены',text)
                sender.reset_mock()
                cb.data='price:detail:0:wrong-token'
                await price_ui.action(cb,state)
                sender.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
