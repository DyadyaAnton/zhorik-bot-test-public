"""Owner-only UI for Stage B staging, manual review and catalog search."""
from __future__ import annotations

import asyncio
import io
import logging

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from database import connect_db
from full_db_backup import create_full_backup
from keyboards import styled
from price_b_core import (MAX_CSV_BYTES, get_candidate, import_csv, init, pending,
                          review, schema_exists, search_catalog, stats)
from build_version import VERSION_LABEL

router = Router(name="price_b_ui")
log = logging.getLogger(__name__)
_awaiting_csv: set[int] = set()


def owner(message):
    target = getattr(message, "message", None) or message
    return bool(message.from_user and message.from_user.id == settings.sandbox_owner_id
                and target.chat.type == "private")


def clear_awaiting(chat_id):
    _awaiting_csv.discard(chat_id)


def ensure_schema(*, already_backed_up=False):
    with connect_db() as db:
        exists = schema_exists(db)
    if not exists and not already_backed_up and settings.db_path.exists():
        create_full_backup()
    with connect_db() as db:
        return init(db)


def _import_csv_with_backup(blob: bytes, filename: str):
    """Run SQLite backup + entire transaction in one worker thread.

    Connections are opened inside the worker (SQLite thread affinity).
    The production connect_db() explicitly sets isolation_level=None.
    """
    if settings.db_path.exists():
        create_full_backup()
    ensure_schema(already_backed_up=True)
    with connect_db() as db:
        return import_csv(db, blob, filename)


def menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(styled("📤 Импорт CSV в очередь проверки", "prb:upload", "primary"))
    kb.row(styled("🔎 Кандидаты для проверки", "prb:pending:0", "primary"))
    kb.row(styled("📋 Справочник продуктов", "prb:catalog"))
    kb.row(styled("✖️ Закрыть", "prb:close", "danger"))
    return kb.as_markup()


def summary():
    ensure_schema()
    with connect_db() as db:
        s = stats(db)
    return ("🧾 Жорик · Этап B · проверка товарных соответствий\n"
            f"Справочник: {s['canonical_products']} продуктов\n"
            f"Импортировано наборов: {s['import_batches']}\n"
            f"В очереди: {s.get('pending',0)}\n"
            f"Подтверждено: {s.get('confirmed',0)}\n"
            f"Отклонено: {s.get('rejected',0)}\n"
            f"Привязок: {s['verified_links']}\n\n"
            "Все цены остаются в изолированном хранилище. "
            "В рабочий прайс и смету ничего не записывается.")


@router.message(Command("price_b"))
async def home(m: Message):
    if owner(m):
        await m.answer(summary(), reply_markup=menu_keyboard())


@router.message(Command("version"))
async def version(m: Message):
    if owner(m):
        await m.answer(VERSION_LABEL)


@router.message(Command("price_b_find"))
async def find(m: Message, command: CommandObject):
    if not owner(m):
        return
    ensure_schema()
    q = (command.args or "").strip()
    if not q:
        await m.answer("Используй: /price_b_find молоко")
        return
    with connect_db() as db:
        rows = search_catalog(db, q)
    content = "\n".join(f"{r[0]} · {r[1]} · ₽/{r[2]}" for r in rows)
    await m.answer("🔎 Результаты:\n" + (content or "Нет совпадений"))


@router.message(Command("price_b_link"))
async def link(m: Message, command: CommandObject):
    if not owner(m):
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].upper().startswith("CP"):
        await m.answer("Формат: /price_b_link 123 CP0001. Сначала найди код через /price_b_find слово")
        return
    ensure_schema()
    try:
        with connect_db() as db:
            result = review(db, int(parts[0]), m.from_user.id, "confirm", parts[1])
        await m.answer(f"✅ Кандидат {result['candidate_id']} привязан к {result['canonical_code']}. "
                       "Это ещё НЕ публикация цены.")
    except ValueError as exc:
        await m.answer("Не подтверждено: " + str(exc)[:180])


def _pending_markup(offset, rows):
    kb = InlineKeyboardBuilder()
    for row in rows:
        kb.row(styled(f"{row['id']} · {row['title'][:29]}", f"prb:detail:{row['id']}"))
    if offset:
        kb.row(styled("⬅️⬅️", f"prb:pending:{max(0,offset-5)}", "primary"))
    if len(rows) == 5:
        kb.row(styled("➡️➡️", f"prb:pending:{offset+5}", "primary"))
    kb.row(styled("⬅️⬅️ Назад", "prb:home", "primary"), styled("✖️ Закрыть", "prb:close", "danger"))
    return kb.as_markup()


def _detail_markup(row):
    kb = InlineKeyboardBuilder()
    if row["suggested_code"] and row["match_status"] != "likely_unrelated" and row["unit_price_rub"]:
        kb.row(styled(f"✅ Подтвердить {row['suggested_code']}", f"prb:confirm:{row['id']}", "success"))
    kb.row(styled("❌ Отклонить", f"prb:reject:{row['id']}", "danger"))
    kb.row(styled("⬅️⬅️ К очереди", "prb:pending:0", "primary"),
           styled("✖️ Закрыть", "prb:close", "danger"))
    return kb.as_markup()


def _detail(row):
    return (f"🔎 Кандидат #{row['id']} · {row['retailer']}\n"
            f"Запрос: {row['query']}\nНазвание: {row['title']}\n"
            f"Цена упаковки: {row['price_rub']} ₽\n"
            f"Цена за единицу: {_display_price(row['unit_price_rub'])} ₽/{row['unit_basis'] or '?'}\n"
            f"Регион: {row['region'] or 'неизвестен'}\n"
            f"Соответствие: {row['match_status']}\n"
            f"Код по точному совпадению: {row['suggested_code'] or 'не определён'}\n\n"
            f"Для ручной привязки найди код через /price_b_find слово, затем\n"
            f"/price_b_link {row['id']} CP0001 (замени CP0001 настоящим кодом).\n"
            "Проверь наименование, фасовку, разновидность и единицы. "
            "При сомнениях не подтверждай.")


def _display_price(raw):
    """Round UI only; keep full precision in SQLite for calculations."""
    from decimal import Decimal, InvalidOperation
    try:
        return f"{Decimal(str(raw)):.2f}" if raw is not None else "нет"
    except (InvalidOperation, ValueError):
        return "нет"


async def _replace_card(call: CallbackQuery, text: str, markup):
    """Edit existing lab card instead of flooding Telegram with new ones."""
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return
        # Some old Telegram cards cannot be edited any more. Keep navigation
        # usable; the fallback creates a new message only on edit failure.
        import logging
        logging.getLogger(__name__).warning("Stage B card edit failed; sending replacement", exc_info=True)
        await call.message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("prb:"))
async def callback(call: CallbackQuery):
    if not call.message or not owner(call):
        await call.answer("Недоступно", show_alert=True)
        return
    await call.answer()
    parts = (call.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "close":
        try:
            await call.message.delete()
        except Exception:
            await call.message.edit_reply_markup(reply_markup=None)
        return
    if action == "upload":
        # Reset both other laboratory input states so the document cannot be
        # swallowed by a TXT or read-only CSV handler.
        from retail_batch_lab import _awaiting as batch_awaiting
        from retail_megalab import _awaiting_csv as lab_awaiting
        batch_awaiting.pop(call.message.chat.id, None)
        lab_awaiting.discard(call.message.chat.id)
        _awaiting_csv.add(call.message.chat.id)
        await call.message.answer("📤 Пришли CSV лаборатории (до 15 МБ). "
                                  "Импорт сохранит кандидатов для проверки, но НЕ изменит рабочие цены.")
        return
    if action == "home":
        await _replace_card(call, summary(), menu_keyboard())
        return
    if action == "catalog":
        await _replace_card(call, "📋 Справочник — 258 базовых продуктов. "
                            "Поиск: /price_b_find молоко\nКоды CP0001…CP0258.", menu_keyboard())
        return
    ensure_schema()
    if action == "pending" and len(parts) == 3 and parts[2].isdigit():
        offset = min(int(parts[2]), 1_000_000)
        with connect_db() as db:
            rows = pending(db, offset)
        names = "\n".join(f"{r['id']}. {r['title'][:70]}" for r in rows)
        await _replace_card(call, f"🔎 Очередь проверки, позиция {offset+1}\n" + (names or "Очередь пуста"),
                            _pending_markup(offset, rows))
    elif action == "detail" and len(parts) == 3 and parts[2].isdigit():
        with connect_db() as db:
            row = get_candidate(db, int(parts[2]))
        if not row or row["review_status"] != "pending":
            await _replace_card(call,"Кандидат уже обработан или отсутствует.\n\n"+summary(),menu_keyboard())
            return
        await _replace_card(call, _detail(row), _detail_markup(row))
    elif action in ("confirm", "reject") and len(parts) == 3 and parts[2].isdigit():
        try:
            with connect_db() as db:
                result = review(db, int(parts[2]), call.from_user.id,
                                "confirm" if action == "confirm" else "reject")
            await _replace_card(call,"✅ Решение сохранено: " + result["action"] + ". Рабочий прайс не изменён.\n\n"+summary(),menu_keyboard())
        except ValueError as exc:
            await _replace_card(call,"Не удалось подтвердить: " + str(exc)[:170],menu_keyboard())


class AwaitingStageBCsv(BaseFilter):
    async def __call__(self, m: Message):
        return owner(m) and m.chat.id in _awaiting_csv


@router.message(F.document, AwaitingStageBCsv())
async def csv_upload(m: Message):
    _awaiting_csv.discard(m.chat.id)
    doc = m.document
    if not doc or not (doc.file_name or "").lower().endswith(".csv") or (doc.file_size or 0) > MAX_CSV_BYTES:
        await m.answer("Нужен CSV в UTF-8, до 15 МБ.")
        return
    if m.chat.id in __import__("lab_run_guard").running_chat_ids:
        await m.answer("Дождись завершения лабораторного теста.")
        return
    try:
        data = io.BytesIO()
        await m.bot.download(doc, destination=data)
        # This is a mass write to new staging tables: use the existing
        # full WAL-safe SQLite backup mechanism first.
        result = await asyncio.to_thread(_import_csv_with_backup,
                                         data.getvalue(), doc.file_name or "batch.csv")
        if result["already_imported"]:
            answer = f"ℹ️ Этот CSV уже импортирован (пакет #{result['batch_id']}). Новых записей нет."
        else:
            answer = (f"✅ Импортирован пакет #{result['batch_id']}. "
                      f"Кандидатов: {result['added']}, пропущено: {result['skipped']}. "
                      "Ничего не опубликовано в рабочем прайсе.")
        await m.answer(answer, reply_markup=menu_keyboard())
    except (ValueError, OSError, UnicodeError, ImportError) as exc:
        log.warning("Stage B CSV rejected: %s", type(exc).__name__)
        await m.answer("CSV не импортирован: " + str(exc)[:150])
