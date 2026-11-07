"""Owner-only Qwen price *hypothesis* lab; never writes working prices.

The configured chat-completions endpoint has no web-search tool here. Thus its
answers are explicitly unverified guesses, not actual retailer quotes.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
import time

from aiogram.types import BufferedInputFile, InlineKeyboardMarkup
from keyboards import styled
from telegram_retry import safe_call

from database import connect_db
from llm_service import BASE, DIAG_ENDPOINTS, LLMError, MODEL, _request
from llm_usage import reserve, settle

_busy = set()
_reports = {}
_TTL = 1800
_BATCH = 12


def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('🔬 Проверить 12 товаров', 'admin:aiprice:start:12')],
        [styled('📦 Весь каталог (до 200)', 'admin:aiprice:start:200')],
        [styled('📄 Скачать отчёт TXT', 'admin:aiprice:report')],
        [styled('⬅️⬅️ Назад', 'admin:menu_back'),
         styled('❌ Закрыть', 'ui:close', 'danger')],
    ])


def panel():
    return ('🔎 Qwen · гипотезы цен\n'
            'Ответы модели НЕ являются актуальными ценами магазинов: '
            'в текущем API нет поиска по сайтам.\n'
            'Не записываются в prices, user_prices или другие рабочие таблицы.\n'
            'Прогон можно использовать для оценки ошибок модели, но не для сметы.')


def catalog(limit=200):
    """Read already populated product catalog, without seeding or mutation."""
    with connect_db() as db:
        rows = db.execute('SELECT name, unit FROM products ORDER BY id LIMIT ?',
                          (max(1, min(int(limit), 200)),)).fetchall()
    return [(str(row['name']), str(row['unit'])) for row in rows]


def validate(value, requested):
    if not isinstance(value, dict) or set(value) != {'estimates'}:
        raise LLMError('Некорректная структура оценки')
    estimates = value['estimates']
    if not isinstance(estimates, list) or len(estimates) > len(requested):
        raise LLMError('Некорректный список оценок')
    lookup = {name: unit for name, unit in requested}
    result = {}
    for row in estimates:
        if not isinstance(row, dict) or set(row) != {'name', 'price_rub', 'store'}:
            raise LLMError('Некорректная позиция оценки')
        name, amount, store = row['name'], row['price_rub'], row['store']
        if name not in lookup or name in result:
            raise LLMError('Неизвестный или повторный товар')
        if amount is not None and (type(amount) not in (int, float) or
                                   not 0 < amount <= 100000):
            raise LLMError('Неправдоподобная оценка')
        if store is not None and (not isinstance(store, str) or len(store) > 50):
            raise LLMError('Некорректное название магазина')
        result[name] = (round(amount, 2) if amount is not None else None, store)
    return [(name, unit, *result.get(name, (None, None))) for name, unit in requested]


def _call(requested):
    token = os.getenv('AMVERA_LLM_API_KEY', '').strip()
    if not token or token.lower().startswith('bearer '):
        raise LLMError('Ключ Qwen не настроен')
    base = os.getenv('AMVERA_LLM_REVIEW_BASE_URL', BASE).rstrip('/')
    if base not in DIAG_ENDPOINTS:
        raise LLMError('Шлюз не разрешён')
    body = {
        'model': MODEL,
        'messages': [
            {'role': 'system', 'content': (
                'Ты участвуешь в лабораторном тесте, а не ищешь реальные цены. '
                'У тебя НЕТ доступа к сайтам магазинов и свежим ценникам. '
                'Дай только приблизительные предположения цен в Москве, '
                'если можешь, либо null; не называй их проверенными. '
                'Верни JSON ровно {"estimates": [{"name": строка, '
                '"price_rub": число или null, "store": строка или null}]}. '
                'Сохраняй названия из запроса буквально, не выдумывай товары.')},
            {'role': 'user', 'content': json.dumps(
                [{'name': name, 'unit': unit} for name, unit in requested],
                ensure_ascii=False)},
        ],
        'temperature': 0, 'response_format': {'type': 'json_object'},
        'max_tokens': 1500,
    }
    data = _request('/chat/completions', token, body, timeout=45, base=base)
    try:
        raw = json.loads(data['choices'][0]['message']['content'])
        validated = validate(raw, requested)
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise LLMError('Qwen не вернул корректную таблицу') from exc
    usage = data.get('usage') or {}
    return validated, usage.get('total_tokens') if isinstance(usage, dict) else None


def report_text(item):
    lines = ['ЖОРИК · AI PRICE LAB · НЕПРОВЕРЕННЫЕ ГИПОТЕЗЫ',
             'Создано UTC: ' + item['created_at'],
             'Нет доступа к интернет-поиску в Qwen API.',
             'Нельзя использовать как актуальные цены, рыночную статистику или смету.',
             'Не внесено ни одной цены в рабочую базу.',
             'Обработано: ' + str(len(item['rows'])),
             'Ошибки партий: ' + str(item['errors']),
             '', 'Товар | Ед. | Гипотеза ₽ | Предположенный магазин']
    for name, unit, amount, store in item['rows']:
        lines.append(f'{name} | {unit} | {amount if amount is not None else "?"} | {store or "?"}')
    return '\n'.join(lines) + '\n'


def latest(owner):
    item = _reports.get(owner)
    if item and time.monotonic() - item['created_mono'] < _TTL:
        return item
    _reports.pop(owner, None)
    return None


async def _job(c, count):
    uid = c.from_user.id
    rows = []
    errors = 0
    reason = 'завершено'
    try:
        try:
            source = await asyncio.to_thread(catalog, count)
            for index in range(0, len(source), _BATCH):
                part = source[index:index + _BATCH]
                rid = await asyncio.to_thread(reserve)
                if not rid:
                    reason = 'остановлено: исчерпан лимит токенов'
                    break
                try:
                    guesses, used = await asyncio.to_thread(_call, part)
                except BaseException:
                    await asyncio.to_thread(settle, rid, None)
                    raise
                await asyncio.to_thread(settle, rid,
                                        used if type(used) is int and used >= 0 else None)
                rows.extend(guesses)
                await safe_call(c.message.edit_text,
                                f'🔎 Гипотезы Qwen: {len(rows)}/{len(source)} товаров…')
        except Exception as exc:
            errors += 1
            reason = 'ошибка партии: ' + type(exc).__name__
        _reports[uid] = {'created_at': datetime.now(timezone.utc).isoformat(),
                         'created_mono': time.monotonic(), 'rows': rows,
                         'errors': errors}
        await safe_call(c.message.edit_text,
                        f'🔎 Проверка завершена: {reason}\n'
                        f'Ответов по товарам: {len(rows)}\n'
                        f'Ошибок: {errors}\n'
                        '⚠️ Это предположения ИИ, не проверенные цены магазинов.',
                        reply_markup=keyboard())
    finally:
        _busy.discard(uid)


async def handle(c, action):
    uid = c.from_user.id
    if action == 'open':
        await c.answer()
        return await safe_call(c.message.edit_text, panel(), reply_markup=keyboard())
    if action == 'report':
        item = latest(uid)
        if not item:
            return await c.answer('Нет свежего отчёта', show_alert=True)
        await c.answer()
        return await safe_call(c.message.answer_document,
                               BufferedInputFile(report_text(item).encode('utf-8'),
                                                 filename='ZHORIK_AI_PRICE_HYPOTHESES.txt'))
    if action.startswith('start:'):
        if uid in _busy:
            return await c.answer('Проверка уже выполняется', show_alert=True)
        try:
            count = int(action.split(':')[1])
        except ValueError:
            return await c.answer('Неправильный размер партии', show_alert=True)
        if count not in (12, 200):
            return await c.answer('Неправильный размер партии', show_alert=True)
        _busy.add(uid)
        try:
            asyncio.create_task(_job(c, count))
        except BaseException:
            _busy.discard(uid)
            raise
        await c.answer('Запущен фоновый тест')
        return
    return await c.answer('Неизвестное действие', show_alert=True)
