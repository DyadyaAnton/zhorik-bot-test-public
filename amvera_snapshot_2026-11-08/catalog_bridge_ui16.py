"""Owner-only view of catalog normalization progress; read-only aggregates."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, Message

from catalog_bridge16 import preview
from catalog_hub16 import summary
from config import settings
from database import connect_db
from keyboards import styled

router = Router(name='catalog_bridge_ui16')
log = logging.getLogger(__name__)


def _render(data, hub=None):
    if not data['schema_ready']:
        return '🔗 Связи каталогов\n\nТребуемые каталоги пока отсутствуют. Слияние не запускалось.'
    c, l = data['counts'], data['links']
    ingredients = l['recipe_ingredients']
    overview = ''
    if hub is not None:
        overview = ('📚 Единые каталоги · состояние\n'
                    f'🛒 Товары: {hub["products"]} · магазинные SKU: {hub["retailer_skus"]}\n'
                    f'💰 Наблюдений цен: {hub["price_observations"]} · личных: {hub["owner_prices"]}\n'
                    f'🥘 Рецепты: {hub["recipes"]}\n'
                    f'📅 Мои меню: {hub["menus"]}\n'
                    f'🔥 КБЖУ в базе: {hub["nutrition_rows"]} '
                    f'(справочных {hub["nutrition_reference"]}, кандидатов {hub["nutrition_candidates"]})\n'
                    f'📋 Мои списки: {hub["owner_lists"]}\n' +
                    (f'⚠️ Отсутствуют таблицы: {", ".join(hub["missing_tables"])}\n' if hub['missing_tables'] else '') + '\n')
    return (overview + '🔗 Связи каталогов · диагностика\n'
            f'📦 Товарный каталог: {c["pb_products"]} позиций\n'
            f'📚 Кандидатный каталог: {c["zb_canonical_products"]} позиций\n'
            f'🍽 Рецептов: {c["recipes"]}\n\n'
            f'✅ Однозначных совпадений товаров: {l["pb_zb_exact_candidates"]}\n'
            f'⚠️ Конфликтов единиц: {l["pb_zb_unit_conflicts"]}\n'
            f'⚠️ Неоднозначных совпадений: {l["pb_zb_ambiguous"]}\n'
            f'🔎 Ингредиентов с точным названием: {ingredients.get("exact_product_name", 0)}\n'
            f'❔ Ингредиентов без точного названия: {ingredients.get("unmatched_name", 0)}\n\n'
            f'📖 Разных несовпадающих названий: {l["unmatched_unique_names"]}\n'
            f'🧩 Упоминаний с вариантами для проверки: {l["with_family_variants_occurrences"]}\n'
            f'🆕 Упоминаний без предложенного варианта: {l["without_family_variants_occurrences"]}\n\n'
            'Это отчёт по названиям, не миграция и не подтверждение КБЖУ или цен.')


def _summary(owner_id):
    with connect_db() as db:
        db.execute('PRAGMA query_only=ON')
        return _render(preview(db), summary(db, owner_id))


@router.message(Command('catalog_links'))
async def catalog_links(m: Message):
    if not m.from_user or m.from_user.id != settings.sandbox_owner_id or m.chat.type != 'private':
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[styled('❌ Закрыть', 'ui:close', 'danger')]])
    try:
        text = await asyncio.wait_for(asyncio.to_thread(_summary, m.from_user.id), timeout=12)
    except Exception as exc:
        log.warning('Catalog links read-only report: %s', type(exc).__name__)
        text = '⚠️ Связи каталогов сейчас недоступны. Данные не изменены.'
    await m.answer(text, reply_markup=keyboard)
