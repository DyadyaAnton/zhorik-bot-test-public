"""Bounded background computations shared by autonomous and AI menu builders.

Cancellation releases the UI task, not a running Python thread. A semaphore is
held until the underlying concurrent Future is actually finished.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='zhorik-menu')
_capacity = BoundedSemaphore(2)


def shutdown_pool():
    """Stop accepting jobs; already running workers complete normally."""
    _pool.shutdown(wait=False,cancel_futures=False)


class WorkersBusy(RuntimeError):
    pass


async def run_heavy(fn, *args, **kwargs):
    if not _capacity.acquire(blocking=False):
        raise WorkersBusy('Все вычислительные слоты заняты; дождитесь завершения предыдущей операции.')
    try:
        future = _pool.submit(fn, *args, **kwargs)
    except BaseException:
        _capacity.release()
        raise
    # Release only when the real worker finishes (also handles cancellation
    # before a queued job starts). Do not tie resource release to coroutine.
    future.add_done_callback(lambda completed: _capacity.release())
    return await asyncio.wrap_future(future)
