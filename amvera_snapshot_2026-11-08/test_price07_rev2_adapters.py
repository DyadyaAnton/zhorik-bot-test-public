import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import price07_rev2_runner as runner
from price07_rev2_core import init, status
from yarche_public_probe import search_products


class SourceAdapterTests(unittest.TestCase):
    def test_yarche_guest_search_bounded_and_unverified(self):
        reply = json.dumps({'data': {'search': {'products': [
            {'id': 'Y01', 'name': 'Молоко 2,5% 950мл', 'price': 99.99},
            {'id': 'Y02', 'name': 'Пустая цена', 'price': None}]}}}).encode()
        def fake_fetch(url, **kwargs):
            if url.endswith('/token'):
                return b'', {'token': 'public-guest'}
            self.assertEqual(kwargs['payload']['variables']['p']['limit'], 2)
            return reply, {}
        with patch('yarche_public_probe.fetch', side_effect=fake_fetch):
            rows = search_products('молоко', limit=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['product_id'], 'Y01')
        self.assertEqual(rows[0]['region'], 'не подтверждён')

    def test_yarche_rejects_bad_query_before_network(self):
        with patch('yarche_public_probe.fetch') as fetch:
            with self.assertRaises(ValueError):
                search_products('\n')
            fetch.assert_not_called()


class QueueRunnerTests(unittest.TestCase):
    def test_five_sources_use_one_shared_cycle_and_restart_cursors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'work.sqlite3'
            def conn():
                return sqlite3.connect(path, isolation_level=None, timeout=5)
            with conn() as db:
                init(db)
            calls = []
            def fetch(source, query):
                calls.append((source, query))
                return []
            with patch.object(runner, 'connect_db', conn):
                first = asyncio.run(runner.cycle(per_source=1, fetch=fetch))
                second = asyncio.run(runner.cycle(per_source=1, fetch=fetch))
            self.assertEqual(first['status'], 'completed')
            self.assertEqual(second['status'], 'completed')
            self.assertEqual(len(calls), 10)
            self.assertEqual(len(set(source for source, _ in calls)), 5)
            with conn() as db:
                progress = {item['source']: item for item in status(db)}
            for source in ('lenta','vkusvill','magnit','pyaterochka','yarche'):
                self.assertEqual(progress[source]['processed'], 2)


if __name__ == '__main__':
    unittest.main()
