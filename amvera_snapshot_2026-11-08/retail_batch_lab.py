"""Owner-only batch search laboratory. Never writes production price tables.

Uses the existing vkusvill_tests.txt list, or an owner-supplied UTF-8 TXT.
Blocked sources are stopped after the first access denial, without retries.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from lab_run_guard import running_chat_ids
from vkusvill_test_lab import load_built_in, parse_queries, MAX_FILE_BYTES
from product_normalizer import normalize, match_product, classify
from keyboards import styled

router = Router(name="retail_batch_lab")
log = logging.getLogger(__name__)
ROOT = Path(settings.data_dir) / "retail_batch_lab"
STORE_NAMES = {"vkusvill": "ВкусВилл", "magnit": "Магнит",
               "pyaterochka": "Пятёрочка", "auchan": "Ашан",
               "perekrestok": "Перекрёсток", "kuper": "Купер", "lenta": "Лента",
               "dixy": "Дикси", "yarche": "Ярче!"}
SEARCH_STORES = {"vkusvill", "magnit", "pyaterochka", "kuper", "lenta"}
MAGNIT_STORE_CODE = "543440"  # Code tested by owner; geographic region unverified.
MAX_QUERIES = 150
PAUSE_SECONDS = 1.5
_active: dict[int, asyncio.Event] = {}
_awaiting: dict[int, str] = {}
_sap_by_chat: dict[int, str] = {}
_kuper_store_by_chat: dict[int, int] = {}


def owner(m) -> bool:
    return bool(m.from_user and m.from_user.id == settings.sandbox_owner_id
                and m.chat.type == "private")


def buttons():
    kb = InlineKeyboardBuilder()
    for code, name in STORE_NAMES.items():
        kb.button(text=("🧪 " if code in SEARCH_STORES or code in ("dixy", "yarche") else "🔎 ") + name,
                  callback_data=f"retbatch:select:{code}")
    kb.button(text="⏹ Остановить", callback_data="retbatch:stop")
    kb.button(text="📥 Последний отчёт", callback_data="retbatch:report")
    kb.adjust(2, 2, 1, 2)
    kb.row(styled("✖️ Закрыть", "retbatch:close", "danger"))
    return kb.as_markup()


async def store_diagnostic(bot, chat_id: int, store: str):
    """One bounded, read-only retailer diagnostic; isolated from normal batch."""
    try:
        # Each retailer owns its diagnostic contract; no shared 'trio' module.
        def check():
            if store == "lenta":
                from lenta_public_probe import diagnose
            elif store == "dixy":
                from dixy_public_probe import diagnose
            elif store == "yarche":
                from yarche_public_probe import diagnose
            else:
                raise ValueError("Неподдерживаемый магазин")
            return diagnose()

        # The HTTP layer has bounded requests; retain the guard until the
        # thread really completes (wait_for would abandon a live thread).
        try:
            details = await asyncio.to_thread(check)
            status = "OK"
        except Exception as exc:
            from retail_probe_http import ProbeError
            from lenta_public_probe import LentaProbeError
            detail = str(exc) if isinstance(exc, (ProbeError, LentaProbeError)) else type(exc).__name__
            details, status = [detail[:160]], "ERROR"
        name = STORE_NAMES[store]
        lines = [f"ЖОРИК — PATCH 09: {name}",
                 "Только диагностические запросы; цены в рабочую БД не записаны.",
                 f"Статус: {status}", *details]
        await bot.send_document(chat_id, BufferedInputFile(
            "\n".join(lines).encode("utf-8"), filename=f"retail_{store}_diagnostic.txt"))
    except Exception as exc:
        log.exception("Retail diagnostic failed: %s", store)
        await bot.send_message(chat_id, "Диагностика " + STORE_NAMES[store] + ": " + type(exc).__name__)
    finally:
        running_chat_ids.discard(chat_id)


def selection_buttons(store: str):
    kb = InlineKeyboardBuilder()
    if store in ("lenta", "dixy", "yarche"):
        kb.button(text="🔬 Одиночная диагностика", callback_data=f"retbatch:probe:{store}")
        if store == "lenta":
            kb.button(text="🔎 Один запрос: молоко", callback_data="retbatch:lentaone:lenta")
            kb.button(text="📤 Загрузить TXT", callback_data="retbatch:upload:lenta")
    elif store in SEARCH_STORES:
        if store != "kuper":
            kb.button(text="▶️ Встроенные 120 запросов", callback_data=f"retbatch:built:{store}")
        kb.button(text="📤 Загрузить TXT", callback_data=f"retbatch:upload:{store}")
        if store == "kuper":
            kb.button(text="🔎 Найти магазины Москвы (1 запрос)", callback_data="retbatch:kuperstores")
    else:
        kb.button(text="🔎 Одна проверка доступности", callback_data=f"retbatch:diag:{store}")
    kb.button(text="⬅️⬅️ Назад", callback_data="retbatch:menu")
    kb.adjust(1)
    kb.row(styled("✖️ Закрыть", "retbatch:close", "danger"))
    return kb.as_markup()


def is_blocked(error: Exception) -> bool:
    """Only known access denials trigger immediate termination of a series."""
    return bool(re.search(r"\b(?:HTTP\s*)?(?:401|403|429)\b|доступ ограничен|captcha|challenge",
                          str(error), flags=re.I))


def offer_dict(item) -> dict | None:
    """Normalize dict or known dataclass observations before both exports."""
    if isinstance(item, dict):
        return item
    if is_dataclass(item) and not isinstance(item, type):
        return asdict(item)
    return None


def enrich_products(products: list[dict], query: str) -> tuple[list[dict], list[dict]]:
    """Parallel sidecars; raw offers remain unchanged if interpretation fails."""
    normalized, matches = [], []
    # The existing shopping-list phrase parser is untouched. A query that is
    # explicitly a basic ingredient implies a raw-ingredient search here;
    # unknown queries remain 'any' rather than forcing a guess.
    query_kind = classify(query)
    intent = "raw_ingredient" if query_kind == "basic_ingredient" else (
        "ready_meal" if query_kind == "prepared_meal" else "any")
    for product in products:
        try:
            item = normalize(product)
            n = item.to_dict()
            title = str(product.get("title") or product.get("name") or "").lower()
            # Never transform kg to litres on an assumed density. Flag the
            # suspicious basis separately from the canonical Phase A warnings.
            n["integration_warnings"] = (["unit_basis_semantically_suspicious"]
                if item.unit_price_basis == "кг" and
                any(x in title for x in ("молоко", "кефир", "йогурт", "сливки")) else [])
            normalized.append(n)
            matches.append(match_product(item, query, intent=intent).to_dict())
        except Exception:
            log.exception("Normalization failed for query %r", query)
            normalized.append({"normalization_failed": True, "warnings": ["normalization_failed"]})
            matches.append({"status": "ambiguous", "reasons": ["normalization_failed"]})
    return normalized, matches


def normalization_stats(records: list[dict]) -> list[str]:
    offers = sum(len(r.get("products", [])) for r in records if r["status"] == "OK")
    normalized = [n for r in records for n in r.get("normalized", [])]
    good = [n for n in normalized if not n.get("normalization_failed")]
    kinds = ("basic_ingredient", "semi_prepared", "prepared_meal", "unknown")
    counts = {k: sum(n.get("product_kind") == k for n in good) for k in kinds}
    priced = sum(n.get("unit_price_rub") is not None for n in good)
    warnings = {}
    for n in good:
        for code in n.get("warnings", []):
            warnings[code] = warnings.get(code, 0) + 1
    match_counts = {s: 0 for s in ("likely_match", "ambiguous", "likely_unrelated")}
    for r in records:
        for m in r.get("matches", []):
            if m.get("status") in match_counts:
                match_counts[m["status"]] += 1
    lines = [f"Нормализация: {len(good)}/{offers} наблюдений",
             "  " + ", ".join(f"{k}: {counts[k]}" for k in kinds),
             f"  unit_price рассчитан: {priced}, не рассчитан: {offers - priced}",
             f"  ошибок нормализации: {len(normalized) - len(good)}",
             "  Сопоставление (intent определён из запроса, не подтверждено): " + ", ".join(
                 f"{k}: {v}" for k, v in match_counts.items()),
             "  warnings:"]
    lines += [f"    {k}: {v}" for k, v in sorted(warnings.items())] or ["    нет"]
    return lines


def csv_safe(value) -> str:
    """Prevent spreadsheet formula execution from untrusted product titles."""
    value = "" if value is None else str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def search_one(store: str, query: str):
    if store == "vkusvill":
        from vkusvill_prices import search
        return search(query)
    if store == "magnit":
        from price_sources_adapters import magnit_probe
        return magnit_probe(MAGNIT_STORE_CODE, query)
    if store == "pyaterochka":
        raise ValueError("Для Пятёрочки нужен SAP-код в параметрах запуска")
    raise ValueError("Пакетный поиск для этого источника не подключён")


def run_search(store: str, query: str, sap: str | None, kuper_store: int | None = None):
    if store == "lenta":
        from lenta_public_probe import search
        return search(query)
    if store == "kuper":
        from kuper_public_probe import search
        if kuper_store is None:
            raise ValueError("Укажи ID магазина через /prices_batch_kuper_store ID")
        return search(kuper_store, query)
    if store == "pyaterochka":
        from pyaterochka_sources_adapter import pyaterochka_probe
        if not sap:
            raise ValueError("SAP-код Пятёрочки не указан")
        return pyaterochka_probe(sap, query)
    return search_one(store, query)


def csv_report(store: str, records: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["store", "query", "status", "product_id", "title", "price_rub",
                     "quantity", "unit", "unit_price_rub", "store_id", "review_status", "error",
                     "old_price_rub", "region", "source_url", "observed_at", "human_volume", "price_type", "retailer_id",
                     "normalized_kind", "package_total", "package_unit", "unit_price_normalized",
                     "unit_price_basis_normalized", "normalization_warnings", "match_status",
                     "match_reasons", "normalization_status"])
    for rec in records:
        products = rec.get("products") if rec["status"] == "OK" else None
        if not products:
            writer.writerow([csv_safe(v) for v in [store, rec["query"], rec["status"], "", "", "", "", "",
                             "", "", "review_required", rec.get("error", ""), "", "", "", "", "", "", ""] + [""] * 9])
            continue
        for idx, p in enumerate(products):
            p = offer_dict(p)
            if p is None:
                continue
            normalized = rec.get("normalized", [])
            matches = rec.get("matches", [])
            n = normalized[idx] if idx < len(normalized) else {}
            match = matches[idx] if idx < len(matches) else {}
            package = n.get("package") or {}
            additions = [n.get("product_kind"), package.get("total_quantity"), package.get("total_unit"),
                         n.get("unit_price_rub"), n.get("unit_price_basis"),
                         ",".join(n.get("warnings", []) + n.get("integration_warnings", [])), match.get("status"),
                         ",".join(match.get("reasons", [])),
                         "failed" if n.get("normalization_failed") else "ok" if n else "not_run"]
            writer.writerow([csv_safe(v) for v in [store, rec["query"], "OK", p.get("product_id") or p.get("id"),
                             p.get("title") or p.get("name"), p.get("price_rub") or p.get("price"),
                             p.get("quantity"), p.get("unit"), p.get("unit_price_rub"),
                             p.get("store_id"), "review_required", "", p.get("old_price_rub"),
                             p.get("region"), p.get("source_url"), p.get("observed_at"),
                             p.get("human_volume"), p.get("price_type"), p.get("retailer_id")] + additions])
    return buffer.getvalue().encode("utf-8-sig")


def format_report(store: str, queries: list[str], records: list[dict],
                  started: str, stop_reason: str) -> bytes:
    successful = sum(x["status"] == "OK" for x in records)
    lines = [f"ЖОРИК — ЛАБОРАТОРИЯ: {STORE_NAMES[store]}",
             f"Начало UTC: {started}", f"Конец UTC: {datetime.now(timezone.utc).isoformat()}",
             f"Запланировано: {len(queries)}", f"Выполнено: {len(records)}",
             f"Технически успешно: {successful}",
             f"Неуспешно: {len(records) - successful}",
             f"Причина остановки: {stop_reason or 'завершено'}",
             "Все предложения требуют проверки соответствия товара и фасовки.",
             "Регион, цена за единицу и доступность акций не считаются подтверждёнными.",
             "Запись в рабочую базу цен отсутствует.", ""]
    lines.extend(normalization_stats(records))
    lines.append("")
    for i, rec in enumerate(records, 1):
        lines.append(f"=== {i:03d}. {rec['query']} ===")
        lines.append(f"Статус: {rec['status']}")
        if rec["status"] == "OK":
            products = rec.get("products", [])
            lines.append(f"Товаров: {len(products)}")
            for j, p in enumerate(products[:10], 1):
                original_type = type(p).__name__
                p = offer_dict(p)
                if p is None:
                    lines.append(f"{j}. Некорректная запись ({original_type})")
                    continue
                name = str(p.get("title") or p.get("name") or "")[:250]
                lines.append(f"{j}. {name}")
                lines.append("   Исходник: " + json.dumps(p, ensure_ascii=False, default=str))
                idx = j - 1
                normalized = rec.get("normalized", [])
                matches = rec.get("matches", [])
                if idx < len(normalized):
                    lines.append("   Нормализация: " + json.dumps(normalized[idx], ensure_ascii=False, default=str))
                if idx < len(matches):
                    lines.append("   Сопоставление: " + json.dumps(matches[idx], ensure_ascii=False, default=str))
        else:
            lines.append(f"Ошибка: {rec.get('error', '')}")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


async def execute(bot, chat_id: int, store: str, queries: list[str], cancel: asyncio.Event,
                  sap: str | None, kuper_store: int | None = None):
    started = datetime.now(timezone.utc).isoformat()
    records: list[dict] = []
    reason = ""
    try:
        if store not in SEARCH_STORES:
            try:
                if store == "auchan":
                    from auchan_html_diag import auchan_html_diagnostic
                    diagnostics = await asyncio.wait_for(asyncio.to_thread(auchan_html_diagnostic), timeout=35)
                elif store == "perekrestok":
                    from perekrestok_api_diag import perekrestok_api_diagnostic
                    diagnostics = await asyncio.wait_for(
                        asyncio.to_thread(perekrestok_api_diagnostic, queries[0]), timeout=35)
                else:
                    raise ValueError("Для этой сети batch-диагностика не предусмотрена")
                status = diagnostics.get("http_status")
                records.append({"query": queries[0], "status": "DIAGNOSTIC",
                                "error": "HTTP " + str(status) + "; " + str(diagnostics.get("note", ""))})
            except Exception as exc:
                records.append({"query": queries[0], "status": "ERROR",
                                "error": type(exc).__name__})
            reason = "Только одна проверка доступности; массовый поиск для этой сети не подключён"
        elif store == "pyaterochka" and not sap:
            reason = "Нужен SAP-код; команда /prices_batch_sap КОД"
        elif store == "kuper" and kuper_store is None:
            reason = "Укажи Store ID Купера: /prices_batch_kuper_store ID; сначала /kuper_stores"
        else:
            for index, q in enumerate(queries, 1):
                if cancel.is_set():
                    reason = "остановлено владельцем"
                    break
                try:
                    rows = await asyncio.wait_for(asyncio.to_thread(run_search, store, q, sap, kuper_store), timeout=35)
                    normalized = [p for row in rows[:10] if (p := offer_dict(row)) is not None]
                    sidecars, matches = enrich_products(normalized, q)
                    records.append({"query": q, "status": "OK", "products": normalized,
                                    "normalized": sidecars, "matches": matches})
                except Exception as exc:
                    log.warning("Retail batch %s query #%s failed: %s", store, index, type(exc).__name__)
                    records.append({"query": q, "status": "ERROR", "error": type(exc).__name__ + ": " + str(exc)[:180]})
                    if is_blocked(exc):
                        reason = "HTTP 401/403/429 или защитная страница; повторов нет"
                        break
                if index % 10 == 0:
                    await bot.send_message(chat_id, f"🧪 {STORE_NAMES[store]}: {index}/{len(queries)}")
                if index < len(queries):
                    try:
                        await asyncio.wait_for(cancel.wait(), timeout=PAUSE_SECONDS)
                    except asyncio.TimeoutError:
                        pass
    except Exception as exc:
        log.exception("Retail batch failed: %s", type(exc).__name__)
        reason = "Внутренняя ошибка " + type(exc).__name__
    finally:
        try:
            try:
                content = format_report(store, queries, records, started, reason)
                csv_content = csv_report(store, records)
            except Exception:
                log.exception("Cannot format retail batch report")
                content = b"Retail batch report formatting failed"
                csv_content = b""
            try:
                ROOT.mkdir(parents=True, exist_ok=True)
                path = ROOT / f"last_{chat_id}.txt"
                temp = ROOT / f"last_{chat_id}.tmp"
                temp.write_bytes(content)
                temp.replace(path)
                (ROOT / f"last_{chat_id}.csv").write_bytes(csv_content)
            except OSError:
                log.exception("Cannot persist retail batch report")
            try:
                await bot.send_document(chat_id, BufferedInputFile(content, filename="retail_batch_results.txt"),
                                        caption=f"🧪 {STORE_NAMES[store]}: {len(records)}/{len(queries)}. "
                                                + (reason or "Готово")[:180])
                if csv_content:
                    await bot.send_document(chat_id, BufferedInputFile(csv_content, filename="retail_batch_results.csv"))
            except Exception:
                log.exception("Cannot send retail batch report")
        finally:
            _active.pop(chat_id, None)
            running_chat_ids.discard(chat_id)


async def start(message: Message, store: str, queries: list[str]):
    if message.chat.id in _active or message.chat.id in running_chat_ids:
        await message.answer("Сначала останови или дождись активного теста.")
        return
    event = asyncio.Event()
    _active[message.chat.id] = event
    running_chat_ids.add(message.chat.id)
    try:
        task = asyncio.create_task(execute(message.bot, message.chat.id, store, queries, event,
                                           _sap_by_chat.get(message.chat.id),
                                           _kuper_store_by_chat.get(message.chat.id)))
        task.add_done_callback(lambda t: log.error("Batch task terminated", exc_info=t.exception())
                               if not t.cancelled() and t.exception() else None)
        await message.answer(f"🧪 {STORE_NAMES[store]}: подготовлено {len(queries)} запросов. "
                             "Отчёт придёт TXT-файлом. Рабочая БД не изменяется.")
    except Exception:
        _active.pop(message.chat.id, None)
        running_chat_ids.discard(message.chat.id)
        raise


@router.message(Command("prices_batch"))
async def menu(m: Message):
    if owner(m):
        _awaiting.pop(m.chat.id, None)
        await m.answer("🧪 Лаборатория магазинов\n"
                       "ВкусВилл и Магнит: пакетный поиск.\n"
                       "Пятёрочка: ожидается подтверждённый SAP-код.\n"
                       "Ашан и Перекрёсток: поисковые серии недоступны (401/403).\n"
                       "Купер: проверка доступности /kuper_stores, затем ID магазина.\n"
                       "Лента: официальный открытый API, сначала один пробный запрос.\n"
                       "Один TXT-запрос на строку, до 150. Выбери сеть:", reply_markup=buttons())


@router.message(Command("kuper_stores"))
async def kuper_stores(m: Message):
    if not owner(m):
        return
    chat_id = m.chat.id
    if chat_id in running_chat_ids:
        await m.answer("Дождись окончания активной лаборатории.")
        return
    running_chat_ids.add(chat_id)
    try:
        from kuper_public_probe import discover_moscow_stores
        rows = await asyncio.wait_for(asyncio.to_thread(discover_moscow_stores), timeout=30)
        text = "🔎 Купер — Москва, центральная точка (не адрес пользователя):\n"
        text += "\n".join(f"{r['store_id']} · {r['retailer']} · {r['name']}" for r in rows)
        kb = InlineKeyboardBuilder()
        for r in rows:
            sid = r["store_id"]
            if sid.isdigit() and 1 <= int(sid) <= 1_000_000_000:
                kb.button(text=(r['retailer'] + ' · ' + r['name'])[:55],
                          callback_data=f"retbatch:kuperid:{sid}")
        kb.adjust(1)
        await m.answer((text or "Магазины не найдены")[:3500],
                       reply_markup=kb.as_markup() if kb.buttons else None)
    except Exception as exc:
        from kuper_public_probe import KuperProbeError
        detail = str(exc)[:120] if isinstance(exc, KuperProbeError) else type(exc).__name__
        await m.answer("Купер: проверка не удалась: " + detail)
    finally:
        running_chat_ids.discard(chat_id)


@router.message(Command("prices_batch_kuper_store"))
async def configure_kuper_store(m: Message):
    if not owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit() or not 1 <= int(parts[1]) <= 1_000_000_000:
        await m.answer("Формат: /prices_batch_kuper_store ID\nСначала /kuper_stores — найди ID нужного магазина.")
        return
    if m.chat.id in running_chat_ids:
        await m.answer("Дождись окончания активного теста.")
        return
    _kuper_store_by_chat[m.chat.id] = int(parts[1])
    await m.answer("Купер: Store ID установлен до перезапуска. /prices_batch → Купер → Загрузить TXT")


@router.message(Command("prices_batch_sap"))
async def configure_sap(m: Message):
    if not owner(m):
        return
    parts = (m.text or "").split()
    if len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", parts[1]):
        await m.answer("Формат: /prices_batch_sap SAP_КОД\nИспользуй реальный код магазина Пятёрочки.")
        return
    if m.chat.id in _active or m.chat.id in running_chat_ids:
        await m.answer("Дождись окончания текущего теста.")
        return
    _sap_by_chat[m.chat.id] = parts[1]
    await m.answer("SAP-код для тестов Пятёрочки установлен (до перезапуска). "
                   "Открой /prices_batch и выбери Пятёрочку.")


@router.callback_query(F.data.startswith("retbatch:"))
async def action(call: CallbackQuery):
    if not call.message or not call.from_user or call.message.chat.type != "private" or call.from_user.id != settings.sandbox_owner_id:
        await call.answer("Недоступно", show_alert=True)
        return
    await call.answer()
    parts = (call.data or "").split(":")
    action_name = parts[1] if len(parts) > 1 else ""
    store = parts[2] if len(parts) > 2 else ""
    if action_name == "menu":
        await call.message.answer("🧪 Выбери магазин:", reply_markup=buttons())
    elif action_name == "close":
        try:
            await call.message.delete()
        except Exception:
            await call.message.edit_reply_markup(reply_markup=None)
    elif action_name == "probe" and store in ("lenta", "dixy", "yarche"):
        chat_id = call.message.chat.id
        if chat_id in running_chat_ids or chat_id in _active:
            await call.message.answer("Дождись завершения текущей лаборатории.")
        else:
            running_chat_ids.add(chat_id)
            task = asyncio.create_task(store_diagnostic(call.bot, chat_id, store))
            task.add_done_callback(lambda t: log.error("Diagnostic task cancelled")
                                   if t.cancelled() else log.error("Diagnostic task failed", exc_info=t.exception())
                                   if t.exception() else None)
            await call.message.answer("🔬 Запущена диагностика: " + STORE_NAMES[store] + ". Отчёт придёт TXT-файлом.")
    elif action_name == "select" and store in STORE_NAMES:
        extra = "\nСначала /kuper_stores и /prices_batch_kuper_store ID." if store == "kuper" else ""
        await call.message.answer("🧪 " + STORE_NAMES[store] + ": выбери тестовый набор." + extra,
                                  reply_markup=selection_buttons(store))
    elif action_name == "kuperstores":
        await call.message.answer("Для поиска магазинов введи /kuper_stores")
    elif action_name == "kuperid" and store.isdigit() and 1 <= int(store) <= 1_000_000_000:
        if call.message.chat.id in running_chat_ids:
            await call.message.answer("Дождись окончания активной лаборатории.")
        else:
            _kuper_store_by_chat[call.message.chat.id] = int(store)
            await call.message.answer(f"✅ Купер: выбран магазин {store}. Теперь 📤 Загрузить TXT.",
                                      reply_markup=selection_buttons("kuper"))
    elif action_name == "built" and store in SEARCH_STORES:
        try:
            await start(call.message, store, load_built_in())
        except (OSError, ValueError) as exc:
            await call.message.answer("Не удалось загрузить встроенный набор: " + str(exc)[:120])
    elif action_name == "diag" and store in ("auchan", "perekrestok"):
        await start(call.message, store, ["молоко"])
    elif action_name == "lentaone" and store == "lenta":
        await start(call.message, "lenta", ["молоко"])
    elif action_name == "upload" and store in SEARCH_STORES:
        if call.message.chat.id in _active or call.message.chat.id in running_chat_ids:
            await call.message.answer("Дождись активного теста.")
        else:
            from price_b_ui import clear_awaiting as clear_stage_b
            from retail_megalab import _awaiting_csv as awaiting_readonly_csv
            clear_stage_b(call.message.chat.id)
            awaiting_readonly_csv.discard(call.message.chat.id)
            _awaiting[call.message.chat.id] = store
            await call.message.answer("📤 Пришли TXT до 64 КБ. Один запрос на строку.")
    elif action_name == "stop":
        event = _active.get(call.message.chat.id)
        if event:
            event.set()
        await call.message.answer("⏹ Остановка запрошена." if event else "Активного теста нет.")
    elif action_name == "report":
        path = ROOT / f"last_{call.message.chat.id}.txt"
        if path.is_file():
            await call.message.answer_document(BufferedInputFile(path.read_bytes(), filename="retail_batch_results.txt"))
            csv_path = ROOT / f"last_{call.message.chat.id}.csv"
            if csv_path.is_file():
                await call.message.answer_document(BufferedInputFile(csv_path.read_bytes(), filename="retail_batch_results.csv"))
        else:
            await call.message.answer("Отчёта пока нет.")


class AwaitingRetailDocument(BaseFilter):
    async def __call__(self, m: Message) -> bool:
        return owner(m) and m.chat.id in _awaiting


@router.message(F.document, AwaitingRetailDocument())
async def upload(m: Message):
    store = _awaiting.pop(m.chat.id, None)
    if store not in SEARCH_STORES:
        return
    if m.chat.id in _active or m.chat.id in running_chat_ids:
        await m.answer("Дождись активного теста.")
        return
    doc = m.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt") or (doc.file_size or 0) > MAX_FILE_BYTES:
        await m.answer("Нужен UTF-8 TXT до 64 КБ.")
        return
    try:
        buffer = io.BytesIO()
        await m.bot.download(doc, destination=buffer)
        queries = parse_queries(buffer.getvalue())
    except Exception as exc:
        await m.answer("Не удалось прочитать TXT: " + str(exc)[:140])
        return
    await start(m, store, queries)


# Independent master laboratory. It is intentionally mounted inside the
# existing owner-only prices router so no main.py modification is necessary.
from price_b_ui import router as price_b_router
router.include_router(price_b_router)
from retail_megalab import router as retail_megalab_router
router.include_router(retail_megalab_router)
