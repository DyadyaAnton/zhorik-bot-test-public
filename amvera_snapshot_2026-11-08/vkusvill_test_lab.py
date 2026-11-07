"""Owner-only batch testing for VkusVill public MCP. No production price writes."""
import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Router, F
from aiogram.filters import Command, BaseFilter
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from config import settings
from vkusvill_prices import search

router = Router(name="vkusvill_test_lab")
log = logging.getLogger(__name__)
ROOT = Path(os.getenv("VKUSVILL_TEST_DIR", "/data/vkusvill_test_lab"))
MAX_QUERIES = 150
MAX_FILE_BYTES = 64_000
PAUSE_SECONDS = 1.5
_active = {}
_awaiting = set()


def owner(message):
    return bool(message.from_user and message.from_user.id == settings.sandbox_owner_id
                and message.chat.type == "private")


def parse_queries(raw):
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("TXT больше 64 КБ")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Нужен TXT в кодировке UTF-8")
    queries = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^\s*\d{1,4}\s*[.)]\s*", "", line).strip()
        if not 2 <= len(line) <= 100:
            raise ValueError(f"Некорректная длина запроса: {line[:50]}")
        queries.append(line)
    if not queries or len(queries) > MAX_QUERIES:
        raise ValueError(f"Нужно от 1 до {MAX_QUERIES} запросов")
    return queries


def keyboard():
    k = InlineKeyboardBuilder()
    k.button(text="📤 Загрузить TXT", callback_data="vvtest:upload")
    k.button(text="▶️ Встроенный набор", callback_data="vvtest:built_in")
    k.button(text="📥 Последний отчёт", callback_data="vvtest:report")
    k.button(text="⏹ Остановить", callback_data="vvtest:stop")
    k.adjust(2)
    return k.as_markup()


def report_text(records, started, stopped=False):
    ok = sum(r["status"] == "OK" for r in records)
    lines = ["ВКУСВИЛЛ — ПАКЕТНЫЙ ТЕСТ", f"Начато (UTC): {started}",
             f"Завершено (UTC): {datetime.now(timezone.utc).isoformat()}",
             f"Обработано: {len(records)}", f"Успешно: {ok}",
             f"Ошибок: {len(records)-ok}", f"Остановлено: {'да' if stopped else 'нет'}",
             "Источник: публичный MCP; цены и наличие по адресу не подтверждены.",
             "Результаты не записываются в пользовательские цены.", ""]
    for n, rec in enumerate(records, 1):
        lines.extend([f"=== ТЕСТ {n:03d} ===", f"Запрос: {rec['query']}",
                      f"Статус: {rec['status']}"])
        if rec["status"] != "OK":
            lines.append(f"Ошибка: {rec['error']}")
        else:
            lines.append(f"Получено товаров: {len(rec['products'])}")
            for i, p in enumerate(rec["products"], 1):
                lines.append(f"{i}. {p.get('name', '')}")
                lines.append(f"   Цена: {p.get('price_rub', '')} ₽; вес: {p.get('weight', '')}; единица: {p.get('unit', '')}")
                lines.append(f"   ID: {p.get('product_id', '')}; источник: {p.get('source', '')}")
                lines.append(f"   Данные: {json.dumps(p, ensure_ascii=False, default=str)}")
        lines.append("")
    return "\n".join(lines)


def load_built_in():
    path = Path(__file__).with_name("vkusvill_tests.txt")
    return parse_queries(path.read_bytes())


async def execute(bot, chat_id, queries, cancel):
    ROOT.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    records = []
    try:
        for idx, q in enumerate(queries):
            if cancel.is_set():
                break
            try:
                rows = await asyncio.to_thread(search, q)
                records.append({"query": q, "status": "OK", "products": rows})
            except Exception as e:
                log.warning("VkusVill lab query failed: %s", q, exc_info=True)
                records.append({"query": q, "status": "ERROR",
                                "error": f"{type(e).__name__}: {str(e)[:300]}"})
            if idx % 10 == 9:
                await bot.send_message(chat_id, f"🧪 Выполнено {idx+1}/{len(queries)}")
            if idx + 1 < len(queries):
                try:
                    await asyncio.wait_for(cancel.wait(), timeout=PAUSE_SECONDS)
                except asyncio.TimeoutError:
                    pass
    finally:
        try:
            content = report_text(records, started, cancel.is_set()).encode("utf-8")
        except Exception:
            log.exception("vv lab: report_text упал")
            content = f"Отчёт не сформирован. Записей: {len(records)}".encode("utf-8")
        try:
            report = ROOT / f"last_{chat_id}.txt"
            temp = ROOT / f"last_{chat_id}.tmp"
            temp.write_bytes(content)
            temp.replace(report)
        except Exception:
            log.exception("vv lab: не удалось сохранить отчёт на диск")
        _active.pop(chat_id, None)
        try:
            await bot.send_document(
                chat_id,
                BufferedInputFile(content, filename="vkusvill_results.txt"),
                caption=(f"🧪 Готово: {len(records)}/{len(queries)} запросов. "
                         f"Ошибок: {sum(r['status'] != 'OK' for r in records)}."),
            )
        except Exception:
            log.exception("vv lab: не удалось отправить отчёт в чат")


async def start_run(message, queries):
    chat_id = message.chat.id
    if chat_id in _active:
        await message.answer("Тест уже выполняется. Используй ⏹ Остановить.")
        return
    cancel = asyncio.Event()
    _active[chat_id] = cancel
    try:
        await message.answer(
            f"🧪 Запущено {len(queries)} запросов. Результаты будут одним TXT. "
            f"Цена и база Жорика не меняются."
        )
        task = asyncio.create_task(execute(message.bot, chat_id, queries, cancel))
    except BaseException:
        _active.pop(chat_id, None)
        raise

    def _on_done(t: asyncio.Task):
        _active.pop(chat_id, None)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.error("vv lab task failed", exc_info=(type(exc), exc, exc.__traceback__))

    task.add_done_callback(_on_done)


@router.message(Command("vkusvill_test"))
async def menu(message: Message):
    if owner(message):
        _awaiting.discard(message.chat.id)
        await message.answer("🧪 Лаборатория ВкусВилла\nTXT: один запрос на строку, до 150 строк, UTF-8.\nВо время тестов публичный MCP может ограничивать частоту запросов.", reply_markup=keyboard())


@router.callback_query(F.data.startswith("vvtest:"))
async def action(call: CallbackQuery):
    if not call.message or call.message.chat.type != "private" or not call.from_user or call.from_user.id != settings.sandbox_owner_id:
        await call.answer("Недоступно", show_alert=True)
        return
    action_name = call.data.partition(":")[2]
    await call.answer()
    if action_name == "upload":
        _awaiting.add(call.message.chat.id)
        await call.message.answer("📤 Пришли TXT-файл с запросами одним документом. /vkusvill_test — меню.")
    elif action_name == "built_in":
        try:
            queries = load_built_in()
        except Exception as e:
            await call.message.answer(f"Не удалось прочитать встроенный набор: {e}")
            return
        await start_run(call.message, queries)
    elif action_name == "stop":
        event = _active.get(call.message.chat.id)
        if event:
            event.set()
        await call.message.answer("⏹ Остановка запрошена. Текущий запрос завершится, затем будет отправлен отчёт." if event else "Активного теста нет.")
    elif action_name == "report":
        path = ROOT / f"last_{call.message.chat.id}.txt"
        if path.is_file():
            await call.message.answer_document(BufferedInputFile(path.read_bytes(), filename="vkusvill_results.txt"))
        else:
            await call.message.answer("Отчёта пока нет.")


class AwaitingVkusvillDocument(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return owner(message) and message.chat.id in _awaiting


@router.message(F.document, AwaitingVkusvillDocument())
async def upload(message: Message):
    if not owner(message) or message.chat.id not in _awaiting:
        return
    if message.chat.id in _active:
        await message.answer("Дождись завершения текущего теста.")
        return
    document = message.document
    if not document.file_name or not document.file_name.lower().endswith(".txt"):
        await message.answer("Нужен файл с расширением .txt.")
        return
    if document.file_size and document.file_size > MAX_FILE_BYTES:
        await message.answer("TXT больше 64 КБ.")
        return
    try:
        buf = io.BytesIO()
        await message.bot.download(document, destination=buf)
        queries = parse_queries(buf.getvalue())
    except Exception as e:
        await message.answer(f"Не удалось прочитать TXT: {e}")
        return
    _awaiting.discard(message.chat.id)
    await start_run(message, queries)
