"""Owner-only view/export of the complete 258-item indicative planning list."""
from __future__ import annotations

import csv
import io
from collections import Counter

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings
from database import connect_db
from keyboards import styled
from planning_prices import catalog_prices

router = Router(name="price_plan_ui")
_levels = {}
_labels = {"economy": "Эконом", "standard": "Стандарт", "comfort": "Комфорт"}


def _owner(user_id, chat_type):
    return user_id == settings.sandbox_owner_id and chat_type == "private"


def _keyboard(level="standard"):
    kb = InlineKeyboardBuilder()
    kb.row(*(styled(("☑️ " if current == level else "") + label,
                    "planprices:level:" + current) for current, label in _labels.items()))
    kb.row(styled("📤 Скачать полный прайс · CSV", "planprices:csv", "primary"))
    kb.row(styled("🔄 Пересчитать по текущим данным", "planprices:refresh", "primary"))
    kb.row(styled("✖️ Закрыть", "planprices:close", "danger"))
    return kb.as_markup()


def _summary(level="standard"):
    with connect_db() as db:
        entries = catalog_prices(db, level)
    quality = Counter(r["quality"] for r in entries.values())
    return ("🧮 Жорик · ориентировочный прайс для меню\n"
            f"Продуктов: {len(entries)} из 258\n"
            f"Ценовой сценарий: {_labels[level]} (только для стартовых оценок)\n"
            f"По лабораторным кандидатам: {quality['candidate_unverified']}\n"
            f"Стартовые расчётные оценки: {quality['planning_assumption']}\n"
            "Лабораторные медианы не меняются при выборе сценария.\n"
            "\n⚠️ Это инструмент планирования, НЕ проверенная рыночная средняя. "
            "Фактическая цена в конкретном магазине зависит от региона, "
            "фасовки, скидок и акций. Жорик не заменяет личную проверку цен.")


@router.message(Command("prices_plan"))
async def open_card(m: Message):
    if m.from_user and _owner(m.from_user.id, m.chat.type):
        level = _levels.get(m.from_user.id, "standard")
        await m.answer(_summary(level), reply_markup=_keyboard(level))


@router.callback_query(F.data.startswith("planprices:"))
async def callback(call: CallbackQuery):
    if not call.message or not call.from_user or not _owner(call.from_user.id, call.message.chat.type):
        await call.answer("Недоступно", show_alert=True)
        return
    parts = (call.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    await call.answer()
    level = _levels.get(call.from_user.id, "standard")
    if action == "level" and len(parts) == 3 and parts[2] in _labels:
        level = parts[2]
        _levels[call.from_user.id] = level
        await call.message.edit_text(_summary(level), reply_markup=_keyboard(level))
        return
    if action == "close":
        try:
            await call.message.delete()
        except Exception:
            await call.message.edit_reply_markup(reply_markup=None)
    elif action == "refresh":
        try:
            await call.message.edit_text(_summary(level), reply_markup=_keyboard(level))
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    elif action == "csv":
        with connect_db() as db:
            entries = catalog_prices(db, level)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(("code", "product", "category", "unit", "price_rub_per_unit",
                         "quality", "source", "observation_count", "level"))
        for entry in entries.values():
            writer.writerow((entry["code"], entry["name"], entry["category"],
                             entry["unit"], entry["price"], entry["quality"],
                             entry["source"], entry["observations"], entry["level"]))
        await call.message.answer_document(
            BufferedInputFile(output.getvalue().encode("utf-8-sig"), filename=f"zhorik_planning_prices_258_{level}.csv"),
            caption="258 расчётных ориентиров; ни один не считается подтверждённой рыночной средней.")
