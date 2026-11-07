"""Owner-only one-click multi-retailer survey, one TXT report, no price writes."""
from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, BaseFilter
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from keyboards import styled
from lab_run_guard import running_chat_ids
from price_intelligence import diagnostic_status

router = Router(name="retail_megalab")
log = logging.getLogger(__name__)
_running_mega: set[int] = set()
_TASKS: set[asyncio.Task] = set()
_awaiting_csv: set[int] = set()
SAMPLES = ("молоко", "яйца", "рис", "яблоки", "куриное филе")


def _owner(chat_id, user_id, chat_type):
    return chat_type == "private" and user_id == settings.sandbox_owner_id


def keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔬 Все источники · короткий тест", callback_data="megaprice:all")
    kb.button(text="📊 Анализ CSV без записи в базу", callback_data="megaprice:csv")
    for store, name in (("vkusvill", "ВкусВилл"), ("magnit", "Магнит"),
                        ("lenta", "Лента"), ("pyaterochka", "Пятёрочка"),
                        ("dixy", "Дикси"), ("yarche", "Ярче!")):
        kb.button(text="🔎 " + name, callback_data="megaprice:one:" + store)
    kb.adjust(1, 1, 2, 2, 2)
    kb.row(styled("✖️ Закрыть", "megaprice:close", "danger"))
    return kb.as_markup()


def _inspect(store, soft_limit_seconds=90):
    """Runs in a worker; source-specific network functions remain independent."""
    from retail_batch_lab import run_search, is_blocked
    from product_normalizer import normalize, match_product
    if store in ("lenta", "dixy", "yarche"):
        if store == "lenta":
            from lenta_public_probe import diagnose
        elif store == "dixy":
            from dixy_public_probe import diagnose
        else:
            from yarche_public_probe import diagnose
        return ["Диагностический запрос:", *diagnose()]
    if store == "pyaterochka":
        # SAP must be tied to an independently verified store, not invented.
        return ["SKIPPED: не выбран подтверждённый SAP-код магазина. Укажи /prices_batch_sap КОД."]
    lines = []
    deadline = time.monotonic() + soft_limit_seconds
    for query in SAMPLES:
        # No asyncio.wait_for here: it would release the chat guard while the
        # HTTP thread is still running. Stop between requests instead.
        if time.monotonic() >= deadline:
            lines.append("SOFT_TIMEOUT: лимит серии истёк; новые запросы не выполнялись")
            break
        try:
            rows = run_search(store, query, None)
            n = len(rows) if isinstance(rows, list) else 0
            known = 0
            mismatches = 0
            for row in (rows if isinstance(rows, list) else [])[:10]:
                try:
                    from retail_batch_lab import offer_dict
                    record = offer_dict(row)
                    if record:
                        item = normalize(record)
                        match = match_product(item, query, "raw_ingredient")
                        known += bool(item.unit_price_rub)
                        mismatches += match.status == "likely_unrelated"
                except (ValueError, TypeError, ImportError, AttributeError):
                    pass
            lines.append(f"{query}: товаров={n}, нормализована_цена={known}, посторонних_кандидатов={mismatches}; ручная проверка обязательна")
        except Exception as exc:
            # Third-party exception messages may contain full URLs or tokens.
            # Keep only the class and access-block flag in owner-visible TXT.
            lines.append(f"{query}: ERROR {type(exc).__name__}" +
                         (" (доступ ограничен)" if is_blocked(exc) else ""))
            if is_blocked(exc):
                lines.append("STOP: источник ограничил доступ; остальные запросы не выполнялись")
                break
    return lines


async def _run(bot, chat_id, stores):
    try:
        lines = ["ЖОРИК — МЕГАЛАБОРАТОРИЯ 10", "UTC: " + datetime.now(timezone.utc).isoformat(),
                 "Только чтение. Записи в рабочую БД цен нет.",
                 "Количество товаров и успех HTTP не подтверждают правильность сопоставления.", ""]
        progress = None
        if len(stores) > 1:
            try:
                progress = await bot.send_message(chat_id, "🔬 Лаборатория: начало проверки сетей…")
            except Exception:
                log.warning("Could not send laboratory progress", exc_info=True)
        for number, store in enumerate(stores, 1):
            if progress is not None:
                try:
                    await progress.edit_text(f"🔬 Проверяю {number}/{len(stores)}: {store}")
                except Exception:
                    log.debug("Could not update progress", exc_info=True)
            lines.append("=== " + store.upper() + " ===")
            try:
                details = await asyncio.to_thread(_inspect, store)
                lines.append("Статус: " + diagnostic_status(details))
                lines.extend(details)
            except Exception as exc:
                log.warning("Mega laboratory failed store=%s type=%s", store, type(exc).__name__)
                lines.append("Статус: ERROR")
                lines.append("ERROR " + type(exc).__name__)
            lines.append("")
        lines.append("ИТОГ: кандидаты требуют проверки; регион/фасовка/акции не подтверждены.")
        await bot.send_document(chat_id, BufferedInputFile("\n".join(lines).encode("utf-8"),
                                filename="zhorik_megalab10_summary.txt"))
        if progress is not None:
            try:
                await progress.delete()
            except Exception:
                pass
    finally:
        _running_mega.discard(chat_id)
        running_chat_ids.discard(chat_id)


@router.message(Command("prices_mega"))
async def menu(m: Message):
    if not m.from_user or not _owner(m.chat.id, m.from_user.id, m.chat.type):
        return
    await m.answer("🧪 Единая лаборатория цен · Patch 10\n"
                   "Все источники: один запуск, один итоговый TXT. Магнит/ВкусВилл — по 5 запросов; "
                   "Лента/Дикси/Ярче! — ограниченная диагностика; Пятёрочка требует SAP. "
                   "Для 100–150 запросов и собственного TXT используй /prices_batch после успешной проверки источника.",
                   reply_markup=keyboard())


@router.callback_query(F.data.startswith("megaprice:"))
async def callback(call: CallbackQuery):
    if not call.message or not call.from_user or not _owner(call.message.chat.id, call.from_user.id, call.message.chat.type):
        await call.answer("Недоступно", show_alert=True)
        return
    action = (call.data or "").split(":")
    await call.answer()
    if len(action) > 1 and action[1] == "close":
        try:
            await call.message.delete()
        except Exception:
            await call.message.edit_reply_markup(reply_markup=None)
        return
    if len(action) > 1 and action[1] == "csv":
        from price_b_ui import clear_awaiting as clear_stage_b
        from retail_batch_lab import _awaiting as awaiting_txt
        clear_stage_b(call.message.chat.id)
        awaiting_txt.pop(call.message.chat.id, None)
        _awaiting_csv.add(call.message.chat.id)
        await call.message.answer("Пришли CSV лаборатории Магнита или ВкусВилла (UTF-8, до 15 МБ). "
                                  "Верну один JSON с результатами нормализации. Рабочая база не изменится.")
        return
    stores = ("vkusvill", "magnit", "lenta", "pyaterochka", "dixy", "yarche") if len(action) > 1 and action[1] == "all" else ()
    if len(action) == 3 and action[1] == "one" and action[2] in ("vkusvill", "magnit", "lenta", "pyaterochka", "dixy", "yarche"):
        stores = (action[2],)
    if not stores:
        return
    chat_id = call.message.chat.id
    if chat_id in _running_mega or chat_id in running_chat_ids:
        await call.message.answer("Дождись завершения активной лаборатории.")
        return
    _running_mega.add(chat_id)
    running_chat_ids.add(chat_id)
    task = asyncio.create_task(_run(call.bot, chat_id, stores))
    _TASKS.add(task)
    def complete(t):
        _TASKS.discard(t)
        if t.cancelled() or t.exception():
            _running_mega.discard(chat_id)
            running_chat_ids.discard(chat_id)
    task.add_done_callback(complete)
    await call.message.answer("🔬 Тест запущен. Получишь один итоговый TXT по выбранным источникам.")


class AwaitingMegaCsv(BaseFilter):
    """Never consume unrelated documents (in particular recipe JSON uploads)."""

    async def __call__(self, m: Message) -> bool:
        return bool(m.from_user and _owner(m.chat.id, m.from_user.id, m.chat.type)
                    and m.chat.id in _awaiting_csv and m.document
                    and (m.document.file_name or '').lower().endswith('.csv'))


@router.message(F.document, AwaitingMegaCsv())
async def csv_upload(m: Message):
    if (not m.from_user or not _owner(m.chat.id, m.from_user.id, m.chat.type)
            or m.chat.id not in _awaiting_csv):
        return
    _awaiting_csv.discard(m.chat.id)
    doc = m.document
    if not doc or not (doc.file_name or "").lower().endswith(".csv") or (doc.file_size or 0) > 15_000_000:
        await m.answer("Нужен CSV в UTF-8, не более 15 МБ.")
        return
    from price_intelligence import report_json
    try:
        data = io.BytesIO()
        await m.bot.download(doc, destination=data)
        report = await asyncio.to_thread(report_json, data.getvalue())
        await m.answer_document(BufferedInputFile(report, filename="zhorik_normalization_report.json"))
    except (ValueError, OSError, UnicodeError, ImportError) as exc:
        await m.answer("Ошибка разбора CSV: " + str(exc)[:160])
