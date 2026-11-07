"""Owner-only PRICE 02 bounded diagnostic command; no background collection."""
from __future__ import annotations

import os
import asyncio

from aiogram import Router
from aiogram.filters import Command

from config import settings
from database import connect_db
from price02_pipeline import ensure, probe, stage, stage_public_candidates, import_legacy_candidates, rollback_if_open
from price_backup import create_full_snapshot
from price06_collect import cursor as price06_cursor, page as price06_page, save_batch as price06_save

router = Router(name='price02_owner')
_active_price_job = None


async def _run_price_job(message, job, *, wait_seconds=50):
    """Reserve before await; retain slot until the *actual job* completes.

    Timeout only stops waiting for the Telegram handler. Shielding keeps the
    tracked job alive, so repeated requests cannot accumulate hidden workers.
    The job itself delivers its eventual success/failure message.
    """
    global _active_price_job
    if _active_price_job is not None and not _active_price_job.done():
        await message.answer('🔬 Проверка цен ещё выполняется. Дождитесь итогового сообщения.')
        return
    task = asyncio.create_task(job())
    _active_price_job = task

    def finish(completed):
        global _active_price_job
        if _active_price_job is completed:
            _active_price_job = None
        try:
            completed.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Price job ended unexpectedly')

    task.add_done_callback(finish)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)
    except asyncio.TimeoutError:
        await message.answer('⏳ Источник отвечает долго. Операция продолжает выполняться; '
                             'новый запуск заблокирован до её фактического завершения.')


def _upgrade_preflight_sync():
    """Read-only preflight + full WAL snapshot, always off event loop."""
    with connect_db() as preflight:
        current = preflight.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='pb_skus'").fetchone()
        needs_upgrade = bool(current and "'lenta'" not in current[0])
    if needs_upgrade:
        create_full_snapshot()


def _legacy_import_job(apply_changes=False):
    # Runs on an isolated worker thread; never holds a Telegram callback open
    # while doing 2,000+ SQLite inserts. An owner must opt in to mutations.
    with connect_db() as db:
        if apply_changes:
            ensure(db)
            db.execute('BEGIN IMMEDIATE')
            try:
                report = import_legacy_candidates(db, dry_run=False, limit=10000)
                db.commit()
                return report
            except BaseException:
                rollback_if_open(db)
                raise
        return import_legacy_candidates(db, dry_run=True, limit=10000)


def _stage_price03_sync(retailer, results):
    _upgrade_preflight_sync()
    with connect_db() as db:
        ensure(db)
        db.execute('BEGIN IMMEDIATE')
        try:
            report = stage_public_candidates(db, retailer, results,
                                             source=f'{retailer}_public_probe')
            db.commit()
            return report
        except BaseException:
            rollback_if_open(db)
            raise


def _stage_price02_sync(query, store_code, results):
    _upgrade_preflight_sync()
    lines = [f'🔬 PRICE 02 · запрос: {query}',
             'Регион не подтверждён; цены пока не используются в смете.']
    with connect_db() as db:
        ensure(db)
        for retailer, rows in results.items():
            if isinstance(rows, Exception):
                lines.append(f'{retailer}: ошибка запроса ({type(rows).__name__}); без повторов')
                continue
            try:
                db.execute('BEGIN IMMEDIATE')
                report = stage(db, retailer, query, rows,
                               store_code=store_code if retailer == 'magnit' else None)
                db.commit()
                lines.append(f'{retailer}: кандидатов {report["staged"]}; '
                             f'на проверку {report["review_required"]}; подтверждено {report["verified"]}')
            except Exception as exc:
                rollback_if_open(db)
                lines.append(f'{retailer}: ошибка обработки ({type(exc).__name__})')
    if 'magnit' not in results:
        lines.append('magnit: пропущен — нужен код магазина (или MAGNIT_STORE_CODE в Amvera)')
    return '\n'.join(lines)


@router.message(Command('price03_import'))
async def price03_import(message):
    """Explicit owner-only, backup-first import of existing lab observations."""
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    args = (message.text or '').partition(' ')[2].strip()
    if args not in ('', 'apply'):
        await message.answer('🔬 /price03_import — только проверка; /price03_import apply — создать полную копию и перенести кандидатов.')
        return
    async def job():
        try:
            if args == 'apply':
                await asyncio.to_thread(create_full_snapshot)
            report = await asyncio.to_thread(_legacy_import_job, args == 'apply')
        except Exception as exc:
            await message.answer(f'⚠️ PRICE 03: перенос не выполнен ({type(exc).__name__}).')
            return
        action = 'перенесено' if args == 'apply' else 'проверено без записи'
        await message.answer(f'🔬 PRICE 03 · исторические кандидаты: {action}.\n'
                             f'Всего {report["total_available"]}; допустимых {report["valid"]}; '
                             f'пропущено {report["skipped"]}.\n'
                             f'Новых SKU {report["inserted_skus"]}; наблюдений {report["inserted_observations"]}.\n'
                             'Все кандидаты неподтверждённые, регион неизвестен; смета не меняется.')
    await _run_price_job(message, job)


@router.message(Command('price03_probe'))
async def price03_probe(message):
    """Owner-only read-only public probe + unverified candidate staging.

    Lenta takes a search term; Auchan requires explicit category and merchant
    ID because the public diagnostic endpoint has no reliable text search.
    Errors never produce fabricated candidates or retry blocked APIs.
    """
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    raw = (message.text or '').partition(' ')[2].strip()
    pieces = raw.split()
    if not pieces or pieces[0] not in ('lenta', 'auchan'):
        await message.answer('🔬 PRICE 03: /price03_probe lenta молоко\n'
                             'Ашан: /price03_probe auchan КАТЕГОРИЯ MERCHANT_ID\n'
                             'Результаты требуют проверки; не используются в смете автоматически.')
        return
    retailer = pieces[0]
    if (retailer == 'lenta' and (len(pieces) < 2 or len(' '.join(pieces[1:])) > 80)) or (
            retailer == 'auchan' and (len(pieces) != 3 or not pieces[2].isdigit())):
        await message.answer('⚠️ PRICE 03: неверные параметры; вызовите /price03_probe для справки.')
        return
    async def job():
        try:
            if retailer == 'lenta':
                from lenta_public_probe import search
                results = await asyncio.to_thread(search, ' '.join(pieces[1:]))
            else:
                from price_sources_adapters import auchan_probe
                results = await asyncio.to_thread(auchan_probe, pieces[1], int(pieces[2]))
        except Exception as exc:
            await message.answer(f'⚠️ PRICE 03 · {retailer}: источник недоступен ({type(exc).__name__}); повторов нет.')
            return
        try:
            report = await asyncio.to_thread(_stage_price03_sync, retailer, results)
        except Exception as exc:
            await message.answer(f'⚠️ PRICE 03: запись не выполнена ({type(exc).__name__}).')
            return
        await message.answer(f'🔬 PRICE 03 · {retailer}: получено {report["received"]}; '
                             f'новых наблюдений {report["inserted_observations"]}; '
                             f'повторов {report["duplicates"]}; '
                             f'пропущено {report["skipped"]}.\n'
                             'Регион и соответствие товара не подтверждены; смета не меняется.')
    await _run_price_job(message, job)


@router.message(Command('price02_probe'))
async def price02_probe(message):
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    query = (message.text or '').partition(' ')[2].strip()
    store_code = os.getenv('MAGNIT_STORE_CODE') or None
    # Optional explicit code for a manual probe, without storing secrets.
    if query.startswith('magnit:'):
        token, _, query = query.partition(' ')
        store_code = token[len('magnit:'):]
        query = query.strip()
    if not query:
        await message.answer('🔬 PRICE 02: /price02_probe молоко\nДля Магнита: /price02_probe magnit:КОД_МАГАЗИНА молоко\nТолько владелец. До 10 предложений на сеть; сомнительные — на проверку.')
        return
    async def job():
        try:
            results = await probe(query, magnit_store_code=store_code)
        except ValueError as exc:
            await message.answer(f'⚠️ PRICE 02: {exc}')
            return
        try:
            summary = await asyncio.to_thread(_stage_price02_sync, query, store_code, results)
        except Exception as exc:
            await message.answer(f'⚠️ PRICE 02: сохранение не выполнено ({type(exc).__name__}). Повторите позже.')
            return
        await message.answer(summary)
    await _run_price_job(message, job)


@router.message(Command('price06_collect'))
async def price06_collect(message):
    """Owner-only five-query page; stored candidates remain unverified."""
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    raw=(message.text or '').partition(' ')[2].strip().lower()
    if raw not in ('','vkusvill','magnit'):
        await message.answer('🔬 PRICE 06: /price06_collect vkusvill или '
                             '/price06_collect magnit. Каждая сеть имеет свой прогресс.')
        return
    retailer=raw or 'vkusvill'

    async def job():
        try:
            # Short transactions and backups belong to a worker thread.
            def prepare():
                _upgrade_preflight_sync()
                with connect_db() as db:
                    return price06_cursor(db,retailer)
            offset=await asyncio.to_thread(prepare)
            terms=price06_page(offset)
            if not terms:
                await message.answer(f'✅ PRICE 06 · {retailer}: все продукты каталога пройдены. '
                                     'Старые кандидаты не становятся подтверждёнными автоматически.')
                return
            await message.answer(f'⏳ PRICE 06 · {retailer}: позиции {offset+1}–{offset+len(terms)}.')
            code=os.getenv('MAGNIT_STORE_CODE') or None
            if retailer=='magnit' and not code:
                await message.answer('⚠️ PRICE 06: укажите MAGNIT_STORE_CODE в Amvera. '
                                     'Курсор Магнита не изменён.')
                return
            observations=[]
            for term in terms:
                try:
                    if retailer=='magnit':
                        from price_sources_adapters import magnit_probe
                        response={'magnit':await asyncio.to_thread(magnit_probe,code,term)}
                    else:
                        response=await probe(term,magnit_store_code=None)
                except Exception as exc:
                    response={retailer:exc}
                observations.append((term,response))

            def persist():
                with connect_db() as db:
                    result=price06_save(db,offset,observations,
                                        retailer=retailer,store_code=code)
                    db.commit()
                    return result
            result=await asyncio.to_thread(persist)
            if result['failed_sources']:
                await message.answer(f'⚠️ PRICE 06 · {retailer}: ошибок '
                                     f'{result["failed_sources"]} из {result["queries"]}; '
                                     'страница не закрыта, повторите позже. '
                                     'Успешные кандидаты сохранены без повышения доверия.')
                return
            await message.answer(f'🔬 PRICE 06 · {retailer}: обработано {result["queries"]} запросов; '
                                 f'кандидатов {result["observations"]}; '
                                 f'ошибок источников {result["failed_sources"]}. '
                                 f'Следующая позиция {result["next_offset"]+1}. '
                                 'Регион неизвестен, рыночные цены не подтверждены.')
        except Exception as exc:
            await message.answer(f'⚠️ PRICE 06: пакет не завершён ({type(exc).__name__}); '
                                 'состояние проверьте перед повтором.')
    await _run_price_job(message,job,wait_seconds=75)
