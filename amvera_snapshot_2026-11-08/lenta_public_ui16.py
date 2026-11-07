"""Owner-only Lenta diagnostic; no writes to any price table."""
import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup

from config import settings
from keyboards import styled
from lenta_public_source16 import stores_for_city, search, LentaSourceError

router = Router(name='lenta_public_ui16')
_running = set()
log = logging.getLogger(__name__)


def _owner(m):
    return bool(m.from_user and m.chat.type == 'private' and
                m.from_user.id == settings.sandbox_owner_id)


def _close():
    return InlineKeyboardMarkup(inline_keyboard=[[styled('❌ Закрыть', 'ui:close', 'danger')]])


@router.message(Command('lenta_stores_probe'))
async def stores(m: Message):
    if not _owner(m):
        return
    if m.chat.id in _running:
        return await m.answer('⏳ Пробник Ленты уже работает.', reply_markup=_close())
    _running.add(m.chat.id)
    try:
        rows = await asyncio.wait_for(asyncio.to_thread(stores_for_city, 'Москва'), timeout=17)
        text = ('🔬 Лента · открытый API · Москва\n' +
                ('\n'.join(f'{r["id"]} · {r["address"]}' for r in rows) if rows
                 else 'Московские магазины не найдены в ответе API.') +
                '\n\n⚠️ Только диагностика; данные не записаны.')
        await m.answer(text[:3800], reply_markup=_close())
    except (LentaSourceError, asyncio.TimeoutError, ValueError) as exc:
        log.warning('Lenta stores diagnostic: %s', type(exc).__name__)
        await m.answer(f'⚠️ Лента · API недоступен ({type(exc).__name__}). Без изменения цен.', reply_markup=_close())
    finally:
        _running.discard(m.chat.id)


@router.message(Command('lenta_price_probe'))
async def products(m: Message):
    if not _owner(m):
        return
    parts = (m.text or '').split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        return await m.answer('Формат: /lenta_price_probe ID_МОСКОВСКОГО_МАГАЗИНА ТОВАР', reply_markup=_close())
    if m.chat.id in _running:
        return await m.answer('⏳ Пробник Ленты уже работает.', reply_markup=_close())
    _running.add(m.chat.id)
    try:
        # The caller chooses the ID from /lenta_stores_probe; never a Tomsk default.
        rows = await asyncio.wait_for(asyncio.to_thread(search, parts[2], int(parts[1])), timeout=17)
        lines = ['🔬 Лента · кандидаты · без записи в БД',
                 f'Магазин: {parts[1]} · найдено: {len(rows)}']
        lines.extend(f'• {r["title"]} — {r["price_rub"] if r["price_rub"] is not None else "нет цены"} ₽'
                     for r in rows)
        lines.append('⚠️ Регион, соответствие товара и цена НЕ подтверждены.')
        await m.answer('\n'.join(lines)[:3800], reply_markup=_close())
    except (LentaSourceError, asyncio.TimeoutError, ValueError) as exc:
        log.warning('Lenta price diagnostic: %s', type(exc).__name__)
        await m.answer(f'⚠️ Лента · поиск недоступен ({type(exc).__name__}). Без изменения цен.', reply_markup=_close())
    finally:
        _running.discard(m.chat.id)
