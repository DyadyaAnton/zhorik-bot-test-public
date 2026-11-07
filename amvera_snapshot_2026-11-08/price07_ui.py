"""Owner-only PRICE07 status and manual weekly trigger."""
import asyncio
import logging
import secrets
import time
from urllib.parse import urlsplit

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram import F

from config import settings
from database import connect_db
from price07_rev2_runner import cycle as rev2_cycle, report as rev2_report
from price07_sku_identity import inspect_sku, assess, package_basis
from price07_sku_review import manual_bind
from price07_nutrition_review import verify_discovered_nutrition
from keyboards import styled

router = Router(name='price07_ui')
log = logging.getLogger(__name__)
_lock = asyncio.Lock()
_pending_sku_reviews = {}
_pending_nutrition_reviews = {}
_SKU_REVIEW_TTL_SECONDS = 600


def _close_markup():
    return InlineKeyboardMarkup(inline_keyboard=[[styled('❌ Закрыть', 'ui:close', 'danger')]])


def _format(report):
    if report is None:
        return '🛒 PRICE-07 · ещё нет завершённых загрузок.'
    if report['status'] == 'already_run':
        return f'🛒 PRICE-07 · неделя {report["week"]} уже запускалась.'
    return ('🛒 PRICE-07 · Лента · недельная очередь\n'
            f'Статус: {report["status"]}\n'
            f'Неделя: {report.get("week", "—")}\n'
            f'Магазин: {report.get("store_id", "—")}\n'
            f'Запросов: {report.get("queries", 0)}\n'
            f'Успешно: {report.get("success", 0)}; ошибок: {report.get("failed", 0)}\n'
            f'Пустых ответов: {report.get("empty", 0)}\n'
            f'Новых SKU: {report.get("new_skus", 0)}\n'
            f'Новых наблюдений: {report.get("new_observations", 0)}\n'
            '⚠️ Цены НЕ подтверждены и не используются автоматически в меню.')


def _latest():
    return rev2_report()


@router.message(Command('price07_status'))
async def price07_status(message: Message):
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    try:
        output = await asyncio.to_thread(_latest)
        await message.answer(_rev2_format(output), reply_markup=_close_markup())
    except Exception as exc:
        log.warning('PRICE07 status unavailable: %s', type(exc).__name__)
        await message.answer('⚠️ PRICE-07: статус пока недоступен; данные не изменены.', reply_markup=_close_markup())


@router.message(Command('price07_refresh'))
async def price07_refresh(message: Message):
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    return await all_refresh(message)


def _rev2_format(rows):
    lines = ['📊 PRICE-07 REV2 · единый каталог цен',
             'Доступные источники обновляются плавно в течение недели.',
             'Новые цены: только кандидаты, без автоподтверждения.', '']
    for row in rows:
        src = row['source']
        if row.get('status') == 'adapter_unavailable':
            lines.append(f'• {src}: интерфейс недоступен')
        elif row.get('status') == 'missing_store_code':
            var = ('PYATEROCHKA_SAP_CODE' if src == 'pyaterochka'
                   else 'MAGNIT_STORE_CODE')
            lines.append(f'• {src}: нужен код магазина {var}')
        else:
            lines.append(f'• {src}: {row["week"] or "не запускался"}; '
                         f'попыток (включая повторы) {row["processed"]}, '
                         f'новых наблюдений (не подтверждены) {row["found"]}, '
                         f'ошибок {row["errors"]}, завершено: {"да" if row["completed"] else "нет"}'
                         + (f'; повтор после {row["retry_after"]}' if row.get('retry_after') else ''))
    return '\n'.join(lines)


@router.message(Command('price07_all_status'))
async def all_status(message: Message):
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    try:
        result = await asyncio.to_thread(rev2_report)
        await message.answer(_rev2_format(result), reply_markup=_close_markup())
    except Exception:
        log.exception('PRICE07 REV2 status')
        await message.answer('⚠️ PRICE07 REV2: статус недоступен.', reply_markup=_close_markup())


@router.message(Command('price07_all_refresh'))
async def all_refresh(message: Message):
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    await message.answer('⏳ PRICE07 REV2 · выполняю очередную порцию обновления.', reply_markup=_close_markup())
    try:
        report = await rev2_cycle(per_source=4)
        if report['status'] == 'busy':
            await message.answer('⏳ Уже идёт обновление цен.', reply_markup=_close_markup())
        else:
            await message.answer(_rev2_format(await asyncio.to_thread(rev2_report)), reply_markup=_close_markup())
    except Exception:
        log.exception('PRICE07 REV2 refresh')
        await message.answer('⚠️ Часть обновления не завершена. Проверьте журнал Amvera.', reply_markup=_close_markup())


def _sku_inspection(sku_id):
    with connect_db() as db:
        return inspect_sku(db, sku_id)


def _sku_review_preview(sku_id, canonical_code):
    with connect_db() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='p7_sku_review_audit'").fetchone():
            raise RuntimeError('SKU review migration has not passed the backup gate')
        sku = db.execute('SELECT title,match_status,product_code FROM pb_skus WHERE id=?',(sku_id,)).fetchone()
        canonical = db.execute('SELECT name,base_unit FROM pb_products WHERE code=?',(canonical_code,)).fetchone()
        if not sku or not canonical or sku[1] != 'review_required' or sku[2] is not None:
            raise ValueError('SKU or canonical product unavailable for review')
        if assess(sku[0],canonical[0])['status'] != 'review_required' or package_basis(sku[0]) != canonical[1]:
            raise ValueError('Exact name and explicit compatible package required')
        return {'sku_title':sku[0],'canonical_title':canonical[0]}


def _sku_review_commit(pending, owner_id):
    evidence = {'confirmed_title': pending['sku_title'], 'confirmed_code': pending['code'],
                'brand_checked': True, 'package_checked': True, 'composition_checked': True,
                'reference': pending['reference'],
                'note': 'Владелец явно подтвердил марку, фасовку и состав после предпросмотра'}
    with connect_db() as db:
        return manual_bind(db,sku_id=pending['sku_id'],canonical_code=pending['code'],
                           actor_id=owner_id,evidence=evidence)


def _valid_evidence_url(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 500 or any(c.isspace() or ord(c)<32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'https' and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443))
    except ValueError:
        return False


@router.message(Command('price07_sku_check'))
async def price07_sku_check(message: Message):
    """Owner-only read-only candidate inspection, never a verification action."""
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    args = (message.text or '').split()
    if len(args) != 2 or not args[1].isascii() or not args[1].isdecimal():
        await message.answer('Формат: /price07_sku_check ID', reply_markup=_close_markup())
        return
    try:
        result = await asyncio.to_thread(_sku_inspection, int(args[1]))
        if result is None:
            await message.answer('SKU не найден; данные не изменены.', reply_markup=_close_markup())
            return
        suggestions = ', '.join(str(item['code']) for item in result['suggestions']) or 'нет точных совпадений'
        lines = ['🔬 SKU · только проверка',
                 f'{result["retailer"]}: {result["title"][:160]}',
                 f'Текущий статус: {result["current_status"]}',
                 f'Кандидаты: {suggestions}']
        if result['catalog_truncated']:
            lines.append('⚠️ Поиск ограничен 5000 позициями; список неполный.')
        if result['truncated']:
            lines.append('⚠️ Показаны первые 5 совпадений.')
        lines.append('⚠️ Совпадение названия не подтверждает товар или цену.')
        await message.answer('\n'.join(lines), reply_markup=_close_markup())
    except Exception:
        log.exception('PRICE07 SKU inspection')
        await message.answer('⚠️ Проверка SKU не удалась; данные не изменены.', reply_markup=_close_markup())


@router.message(Command('price07_sku_bind'))
async def price07_sku_bind_prepare(message: Message):
    """First phase: owner reviews exact SKU, canonical identity and evidence."""
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id or message.chat.type != 'private':
        return
    parts = (message.text or '').split(maxsplit=3)
    if (len(parts) != 4 or not parts[1].isascii() or not parts[1].isdecimal()
            or not _valid_evidence_url(parts[3].strip())):
        await message.answer('Формат: /price07_sku_bind ID CANONICAL_CODE ССЫЛКА_НА_ЭТИКЕТКУ',reply_markup=_close_markup())
        return
    try:
        sku_id = int(parts[1]);code=parts[2]
        preview = await asyncio.to_thread(_sku_review_preview,sku_id,code)
        now = time.monotonic()
        for key,item in tuple(_pending_sku_reviews.items()):
            if now-item['created_at'] > _SKU_REVIEW_TTL_SECONDS:
                _pending_sku_reviews.pop(key,None)
        if len(_pending_sku_reviews)>=20:
            await message.answer('Слишком много неподтверждённых проверок, повторите позже.',reply_markup=_close_markup())
            return
        token=secrets.token_hex(12)
        _pending_sku_reviews[token]={**preview,'sku_id':sku_id,'code':code,
                                     'reference':parts[3].strip(),'owner_id':message.from_user.id,
                                     'chat_id':message.chat.id,'created_at':now}
        keyboard=InlineKeyboardMarkup(inline_keyboard=[
            [styled('✅ Проверил марку, фасовку и состав',f'p7:bind:{token}','success')],
            [styled('❌ Закрыть','ui:close','danger')]])
        await message.answer('🔎 Ручная проверка SKU — предпросмотр\n'
                             f'SKU #{sku_id}: {preview["sku_title"][:150]}\n'
                             f'Канон {code}: {preview["canonical_title"][:150]}\n'
                             f'Источник этикетки: {parts[3].strip()[:180]}\n'
                             'Подтверждая, вы лично удостоверяете проверку марки, фасовки и состава.\n'
                             'Ссылка сохраняется для аудита, но не проверяется автоматически.\n'
                             'Цена, регион, КБЖУ и аллергены НЕ подтверждаются.',reply_markup=keyboard)
    except Exception as exc:
        log.warning('PRICE07 SKU review preview rejected: %s',type(exc).__name__)
        await message.answer('Проверка не подготовлена: нет миграции, точного соответствия или доказательств.',reply_markup=_close_markup())


@router.callback_query(F.data.startswith('p7:bind:'))
async def price07_sku_bind_confirm(callback: CallbackQuery):
    """Second phase: single-use, owner/chat-bound, expiring confirmation."""
    if not callback.from_user or callback.from_user.id != settings.sandbox_owner_id or not callback.message or callback.message.chat.type != 'private':
        await callback.answer('Недоступно',show_alert=True)
        return
    token=(callback.data or '').removeprefix('p7:bind:')
    pending=_pending_sku_reviews.get(token)
    if (not pending or pending['owner_id'] != callback.from_user.id
            or pending['chat_id'] != callback.message.chat.id):
        await callback.answer('Подтверждение устарело; подготовьте новое.',show_alert=True)
        return
    _pending_sku_reviews.pop(token,None)
    if time.monotonic()-pending['created_at'] > _SKU_REVIEW_TTL_SECONDS:
        await callback.answer('Подтверждение просрочено; подготовьте новое.',show_alert=True)
        return
    try:
        await asyncio.to_thread(_sku_review_commit,pending,callback.from_user.id)
        await callback.answer('Проверка записана')
        await callback.message.answer('✅ SKU связан с каноном вручную. Цены и КБЖУ не подтверждены.',reply_markup=_close_markup())
    except Exception:
        log.exception('PRICE07 manual SKU binding failed')
        await callback.answer('Связь не сохранена; проверьте журнал и повторите проверку.',show_alert=True)


def _nutrition_review_preview(code):
    with connect_db() as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'p7_nutrition_review','p7_nutrition_review_audit'} <= tables:
            raise RuntimeError('Nutrition review migration has not passed backup gate')
        row=db.execute('''SELECT canonical_name,review_status,kbju_verified,allergens_verified
                          FROM p7_discovered_products WHERE code=?''',(code,)).fetchone()
        if row is None or row[1]!='unverified' or row[2] or row[3]:
            raise ValueError('Product is not available for a first review')
        return row[0]


def _nutrition_review_commit(pending, owner_id):
    with connect_db() as db:
        return verify_discovered_nutrition(db,code=pending['code'],actor_id=owner_id,
            kcal_100g=pending['kcal'],protein_100g=pending['protein'],
            fat_100g=pending['fat'],carbs_100g=pending['carbs'],
            allergens=pending['allergens'],source=pending['source'],
            note='Владелец сверил КБЖУ на 100 г и полный состав по указанному источнику')


@router.message(Command('price07_nutrition_verify'))
async def price07_nutrition_verify_prepare(message: Message):
    """Owner-only manual preview: no unreviewed nutrition enters menus."""
    if (not message.from_user or message.from_user.id != settings.sandbox_owner_id
            or message.chat.type != 'private'):
        return
    parts=(message.text or '').split()
    if len(parts)!=8:
        await message.answer('Формат: /price07_nutrition_verify NP_CODE ККАЛ Б Ж У АЛЛЕРГЕНЫ HTTPS_ИСТОЧНИК\n'
                             'Аллергены — через запятую; если состав проверен и их нет, укажите дефис (-).',
                             reply_markup=_close_markup())
        return
    try:
        code=parts[1]
        numeric=[float(value.replace(',','.')) for value in parts[2:6]]
        allergens=[] if parts[6]=='-' else [a.strip() for a in parts[6].split(',')]
        from price07_nutrition_review import _validate
        normalized=_validate(code,message.from_user.id,*numeric,allergens,parts[7],
                             'Владелец сверил КБЖУ на 100 г и полный состав по указанному источнику')
        name=await asyncio.to_thread(_nutrition_review_preview,code)
        now=time.monotonic()
        for key,item in tuple(_pending_nutrition_reviews.items()):
            if now-item['created_at']>_SKU_REVIEW_TTL_SECONDS:
                _pending_nutrition_reviews.pop(key,None)
        if len(_pending_nutrition_reviews)>=20:
            raise RuntimeError('Too many pending owner reviews')
        token=secrets.token_hex(12)
        _pending_nutrition_reviews[token]={
            'code':code,'kcal':numeric[0],'protein':numeric[1],
            'fat':numeric[2],'carbs':numeric[3],'allergens':normalized,
            'source':parts[7],'owner_id':message.from_user.id,
            'chat_id':message.chat.id,'created_at':now}
        keyboard=InlineKeyboardMarkup(inline_keyboard=[
            [styled('✅ Проверил КБЖУ и весь состав',f'p7:nutrition:{token}','success')],
            [styled('❌ Закрыть','ui:close','danger')]])
        await message.answer(f'🥦 Проверка нового продукта — предпросмотр\n{name}\n{code}\n'
                             f'На 100 г: {numeric[0]:g} ккал · Б {numeric[1]:g} · '
                             f'Ж {numeric[2]:g} · У {numeric[3]:g}\n'
                             f'Проверенные аллергены: {", ".join(normalized) or "отсутствуют"}\n'
                             f'Источник: {parts[7][:180]}\n'
                             'Подтверждение сохраняет аудит. Сама ссылка не проверяется автоматически. '
                             'Это не медицинская гарантия.',reply_markup=keyboard)
    except (ValueError,RuntimeError) as exc:
        log.warning('PRICE07 nutrition preview rejected: %s',type(exc).__name__)
        await message.answer('Проверка не подготовлена: проверьте данные, ссылку или миграцию после backup.',
                             reply_markup=_close_markup())


@router.callback_query(F.data.startswith('p7:nutrition:'))
async def price07_nutrition_verify_confirm(callback: CallbackQuery):
    if (not callback.from_user or callback.from_user.id != settings.sandbox_owner_id
            or not callback.message or callback.message.chat.type!='private'):
        return await callback.answer('Недоступно',show_alert=True)
    token=(callback.data or '').removeprefix('p7:nutrition:')
    pending=_pending_nutrition_reviews.get(token)
    if (not pending or pending['owner_id']!=callback.from_user.id
            or pending['chat_id']!=callback.message.chat.id):
        return await callback.answer('Проверка устарела.',show_alert=True)
    _pending_nutrition_reviews.pop(token,None)
    if time.monotonic()-pending['created_at']>_SKU_REVIEW_TTL_SECONDS:
        return await callback.answer('Время подтверждения истекло.',show_alert=True)
    try:
        await asyncio.to_thread(_nutrition_review_commit,pending,callback.from_user.id)
        await callback.answer('Проверка записана')
        await callback.message.answer('✅ КБЖУ и состав записаны с источником и аудитом. '
                                      'Добавление в рецепты — отдельный этап.',reply_markup=_close_markup())
    except Exception:
        log.exception('PRICE07 nutrition review failed')
        await callback.answer('Не сохранено; проверьте журнал и данные.',show_alert=True)
