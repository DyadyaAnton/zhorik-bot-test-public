"""Ограниченные повторы отправки сообщений при временных сетевых ошибках."""
import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramRetryAfter

T = TypeVar("T")
log = logging.getLogger("zhorik.telegram_retry")


async def safe_call(func: Callable[..., Awaitable[T]], *args: Any, attempts: int = 3, **kwargs: Any) -> T:
    last_error = None
    for attempt in range(attempts):
        try:
            return await func(*args, **kwargs)
        except TelegramRetryAfter as exc:
            last_error = exc
            delay = min(float(exc.retry_after), 30.0)
        except (TelegramNetworkError, TelegramServerError) as exc:
            last_error = exc
            delay = min(0.7 * (2 ** attempt), 5.0)
        if attempt + 1 < attempts:
            log.warning("Временная ошибка Telegram; повтор %s/%s через %.1f с", attempt + 1, attempts, delay)
            from perf_diagnostics import add as perf_add
            waited=time.perf_counter()
            await asyncio.sleep(delay)
            perf_add("telegram_retry_wait",time.perf_counter()-waited)
    raise last_error


async def safe_send_message(bot, chat_id: int, text: str, **kwargs: Any):
    return await safe_call(bot.send_message, chat_id, text, **kwargs)
