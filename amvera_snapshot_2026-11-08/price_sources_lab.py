"""Жорик: owner-only диагностика Магнита и Ашана, отдельная от цен пользователей."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from config import settings
from price_sources_adapters import magnit_probe, magnit_categories_probe, magnit_structure_probe, auchan_probe
from price_sources_core import csv_bytes
from pyaterochka_sources_adapter import pyaterochka_probe
from perekrestok_sources_adapter import perekrestok_probe, PerekrestokProbeError
from perekrestok_api_diag import perekrestok_api_diagnostic
from auchan_html_diag import auchan_html_diagnostic

router = Router(name='price_sources_lab')
log = logging.getLogger(__name__)
_running = set()


def _owner(m):
    return bool(m.from_user and m.from_user.id == settings.sandbox_owner_id and m.chat.type == 'private')


@router.message(Command('sources_lab'))
async def help_sources(m: Message):
    if not _owner(m):
        return
    await m.answer('🔬 Новые источники цен (только тест, без записи в рабочую БД).\n'
                   '/magnit_categories КОД_МАГАЗИНА\n'
                   '/magnit_structure КОД_МАГАЗИНА\n'
                   '/magnit_api_test КОД_МАГАЗИНА ПОИСКОВАЯ_ФРАЗА\n'
                   '/auchan_api_test КАТЕГОРИЯ MERCHANT_ID\n'
                   '/auchan_html_test\n'
                   '/pyaterochka_api_test SAP_КОД ПОИСКОВАЯ_ФРАЗА\n'
                   '/perekrestok_api_test /cat/c/ID/nazvanie\n'
                   '/perekrestok_json_test ПОИСКОВАЯ_ФРАЗА\n'
                   'Неофициальные интерфейсы. Один запрос, максимум 5 товаров. '
                   'Регион не подтверждён; все результаты требуют проверки.\n'
                   'Пятёрочка: одиночный HTTPS-тест, без браузера и обхода ограничений.')


async def _probe(m: Message, store: str):
    if not _owner(m):
        return
    parts = (m.text or '').split(maxsplit=2)
    if len(parts) != 3 or not parts[2].strip():
        await m.answer('Формат: /magnit_api_test КОД_МАГАЗИНА ПОИСКОВАЯ_ФРАЗА' if store == 'magnit'
                       else 'Формат: /auchan_api_test КАТЕГОРИЯ MERCHANT_ID')
        return
    if m.chat.id in _running:
        await m.answer('Предыдущий тест ещё выполняется.')
        return
    if store == 'auchan':
        # Validate before starting the background operation.
        try:
            merchant_id = int(parts[2])
        except ValueError:
            await m.answer('MERCHANT_ID должен быть целым числом.')
            return
    _running.add(m.chat.id)
    try:
        if store == 'magnit':
            func, args = magnit_probe, (parts[1], parts[2])
        else:
            func, args = auchan_probe, (parts[1], merchant_id)
        rows = await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=35)
        await m.answer(f'🔬 {store}: получено {len(rows)} предложений. '
                       'Все — review_required; рабочая БД не изменена.')
        if rows:
            await m.answer_document(BufferedInputFile(csv_bytes(rows), filename=f'{store}_api_probe.csv'))
    except Exception as exc:
        log.warning('Price source probe %s: %s', store, type(exc).__name__)
        await m.answer(f'🔬 {store}: тест не удался: {type(exc).__name__}: {str(exc)[:240]}')
    finally:
        _running.discard(m.chat.id)


@router.message(Command('perekrestok_json_test'))
async def perekrestok_json_test(m: Message):
    """One bounded read-only request to the catalog API (not the HTML page)."""
    if not _owner(m):
        return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        await m.answer('Формат: /perekrestok_json_test ПОИСКОВАЯ_ФРАЗА')
        return
    if m.chat.id in _running:
        await m.answer('Предыдущий тест ещё выполняется.')
        return
    _running.add(m.chat.id)
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(perekrestok_api_diagnostic, parts[1]), timeout=30
        )
        import json
        await m.answer('🔬 perekrestok JSON API: ' + json.dumps(
            info, ensure_ascii=False, sort_keys=True
        )[:1500] + '\nРабочая БД не изменена.')
    except ValueError as exc:
        await m.answer('🔬 perekrestok: ' + str(exc)[:160])
    except Exception as exc:
        log.warning('Perekrestok JSON diagnostic failed: %s', type(exc).__name__)
        await m.answer('🔬 perekrestok: ошибка ' + type(exc).__name__)
    finally:
        _running.discard(m.chat.id)


@router.message(Command('magnit_api_test'))
async def magnit_test(m: Message):
    await _probe(m, 'magnit')


@router.message(Command('auchan_html_test'))
async def auchan_html_test(m: Message):
    if not _owner(m):
        return
    if (m.text or '').strip() != '/auchan_html_test':
        await m.answer('Формат: /auchan_html_test')
        return
    if m.chat.id in _running:
        await m.answer('Предыдущий тест ещё выполняется.')
        return
    _running.add(m.chat.id)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(auchan_html_diagnostic), timeout=30
        )
        import json
        await m.answer('🔬 auchan HTML: ' + json.dumps(
            result, ensure_ascii=False, sort_keys=True
        ) + '\nРабочая БД не изменена.')
    except Exception as exc:
        log.warning('Auchan HTML diagnostic failed: %s', type(exc).__name__)
        await m.answer('🔬 auchan: ошибка ' + type(exc).__name__)
    finally:
        _running.discard(m.chat.id)


@router.message(Command('auchan_api_test'))
async def auchan_test(m: Message):
    await _probe(m, 'auchan')


@router.message(Command('magnit_categories'))
async def magnit_categories(m: Message):
    if not _owner(m):
        return
    parts = (m.text or '').split()
    if len(parts) != 2:
        await m.answer('Формат: /magnit_categories КОД_МАГАЗИНА')
        return
    if m.chat.id in _running:
        await m.answer('Предыдущий тест ещё выполняется.')
        return
    _running.add(m.chat.id)
    try:
        cats = await asyncio.wait_for(asyncio.to_thread(magnit_categories_probe, parts[1]), timeout=35)
        if not cats:
            await m.answer('🔬 magnit: запрос прошёл, но категории не найдены.')
        else:
            lines = [f"{x['id'] or 'ID неизвестен'} — {x['name'] or 'Без названия'}" for x in cats]
            await m.answer('🔬 magnit: категории (максимум 5):\n' + '\n'.join(lines))
    except Exception as exc:
        log.warning('Magnit category probe: %s', exc)
        await m.answer(f'🔬 magnit: категории не получены: {type(exc).__name__}: {str(exc)[:240]}')
    finally:
        _running.discard(m.chat.id)


@router.message(Command('magnit_structure'))
async def magnit_structure(m: Message):
    if not _owner(m):
        return
    parts = (m.text or '').split()
    if len(parts) != 2:
        await m.answer('Формат: /magnit_structure КОД_МАГАЗИНА')
        return
    if m.chat.id in _running:
        await m.answer('Предыдущий тест ещё выполняется.')
        return
    _running.add(m.chat.id)
    try:
        import json
        result = await asyncio.wait_for(asyncio.to_thread(magnit_structure_probe, parts[1]), timeout=35)
        body = json.dumps(result, ensure_ascii=False, indent=2)
        await m.answer('🔬 magnit: структура ответа (значения скрыты):')
        await m.answer_document(BufferedInputFile(body.encode('utf-8'), filename='magnit_structure.json'))
    except Exception as exc:
        log.warning('Magnit structure probe: %s', type(exc).__name__)
        # Avoid exposing response bodies or potentially sensitive URLs from exceptions.
        await m.answer(f'🔬 magnit: диагностика не удалась: {type(exc).__name__}')
    finally:
        _running.discard(m.chat.id)


@router.message(Command('pyaterochka_api_test'))
async def pyaterochka_api_test(m: Message):
    if not _owner(m):
        return
    parts = (m.text or '').split(maxsplit=2)
    if len(parts) != 3 or not parts[2].strip():
        await m.answer('Формат: /pyaterochka_api_test SAP_КОД ПОИСКОВАЯ_ФРАЗА')
        return
    if m.chat.id in _running:
        await m.answer('Предыдущий тест ещё выполняется.')
        return
    _running.add(m.chat.id)
    try:
        rows = await asyncio.wait_for(
            asyncio.to_thread(pyaterochka_probe, parts[1], parts[2]), timeout=30
        )
        await m.answer(f'🔬 pyaterochka: получено {len(rows)} предложений. '
                       'Все — review_required; рабочая БД не изменена. '
                       'Регион и применимость скидок не подтверждены.')
        if rows:
            await m.answer_document(BufferedInputFile(
                csv_bytes(rows), filename='pyaterochka_api_probe.csv'
            ))
    except Exception as exc:
        log.warning('Pyaterochka probe: %s', type(exc).__name__)
        # Не раскрываем URL с параметрами, cookies или ответ сервера.
        from pyaterochka_sources_adapter import PyaterochkaProbeError
        if isinstance(exc, (PyaterochkaProbeError, ValueError)):
            await m.answer(f'🔬 pyaterochka: {str(exc)[:220]}')
        else:
            await m.answer(f'🔬 pyaterochka: тест не удался: {type(exc).__name__}')
    finally:
        _running.discard(m.chat.id)


@router.message(Command('perekrestok_api_test'))
async def perekrestok_api_test(m: Message):
    if not _owner(m):
        return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) != 2:
        await m.answer('Формат: /perekrestok_api_test /cat/c/ID/nazvanie')
        return
    if m.chat.id in _running:
        await m.answer('Предыдущий тест ещё выполняется.')
        return
    _running.add(m.chat.id)
    try:
        info, rows = await asyncio.wait_for(asyncio.to_thread(perekrestok_probe, parts[1]), timeout=30)
        await m.answer('🔬 perekrestok: HTTP 200, HTML ' + str(info['html_bytes']) +
                       ' байт, JSON-LD блоков: ' + str(info['jsonld_blocks']) +
                       ', товаров: ' + str(len(rows)) + '.\n' + info['note'] +
                       '\nРабочая БД не изменена; регион не подтверждён.')
        if rows:
            await m.answer_document(BufferedInputFile(csv_bytes(rows), filename='perekrestok_api_probe.csv'))
    except (PerekrestokProbeError, ValueError) as exc:
        await m.answer('🔬 perekrestok: ' + str(exc)[:200])
    except Exception as exc:
        log.warning('Perekrestok probe failed: %s', type(exc).__name__)
        await m.answer('🔬 perekrestok: ошибка ' + type(exc).__name__)
    finally:
        _running.discard(m.chat.id)
