"""Regression tests for timeout/cancellation without a real Telegram token."""
import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import patch


def load_handler_module():
    fake_aiogram = types.ModuleType('aiogram')
    fake_filters = types.ModuleType('aiogram.filters')

    class Router:
        def __init__(self, name):
            self.name = name

        def message(self, *args, **kwargs):
            return lambda function: function

    fake_aiogram.Router = Router
    fake_filters.Command = lambda *args, **kwargs: None
    with patch.dict(sys.modules, {'aiogram': fake_aiogram, 'aiogram.filters': fake_filters}):
        spec = importlib.util.spec_from_file_location('price03_async_under_test',
                    __file__.replace('test_price03_async_guard.py', 'price02_ui.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class DummyMessage:
    def __init__(self):
        self.responses = []

    async def answer(self, value):
        self.responses.append(value)


class PriceJobGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ui = load_handler_module()

    async def test_timeout_never_releases_running_worker_slot(self):
        done = asyncio.Event()
        starts = []
        async def long_job():
            starts.append('running')
            await done.wait()
        first = DummyMessage()
        await self.ui._run_price_job(first, long_job, wait_seconds=0.01)
        self.assertFalse(self.ui._active_price_job.done())
        second = DummyMessage()
        await self.ui._run_price_job(second, long_job, wait_seconds=0.01)
        self.assertEqual(starts, ['running'])
        self.assertIn('выполняется', second.responses[0])
        done.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        third = DummyMessage()
        await self.ui._run_price_job(third, long_job, wait_seconds=0.1)
        self.assertEqual(len(starts), 2)

    async def test_handler_cancel_keeps_underlying_job_tracked(self):
        release = asyncio.Event()
        async def work():
            await release.wait()
        wrapper = asyncio.create_task(self.ui._run_price_job(DummyMessage(), work))
        await asyncio.sleep(0)
        wrapper.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await wrapper
        self.assertIsNotNone(self.ui._active_price_job)
        self.assertFalse(self.ui._active_price_job.done())
        reject = DummyMessage()
        await self.ui._run_price_job(reject, work)
        self.assertIn('выполняется', reject.responses[0])
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


if __name__ == '__main__':
    unittest.main()
