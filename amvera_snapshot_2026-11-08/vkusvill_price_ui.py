"""Owner-only VkusVill read-only search inside Zhorik."""
import asyncio
import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from config import settings
from database import connect_db
from vkusvill_prices import search,save

router=Router(name='vkusvill_price_ui')
log=logging.getLogger(__name__)

@router.message(Command('vkusvill_prices'))
async def lookup(message:Message):
    if not message.from_user or message.from_user.id != settings.sandbox_owner_id:
        return
    query=(message.text or '').partition(' ')[2].strip()
    if not query:
        await message.answer('🔎 Проверка ВкусВилла: /vkusvill_prices молоко\nТолько владелец. Результаты не используются в смете меню.')
        return
    try:
        rows=await asyncio.to_thread(search,query)
        if rows:
            with connect_db() as db:
                save(db,rows)
        lines=[f'🛒 ВкусВилл: {query}', 'Публичный каталог: цены и наличие по адресу не подтверждены. Цена за кг/л справочная.']
        for row in rows[:8]:
            suffix = ''
            if row.get('unit_price_status') == 'estimated_public_catalog':
                suffix = f" · ≈ {row['price_per_base_unit']:g} ₽/{row['base_unit']}"
            if row.get('category_status') == 'prepared_or_mixed':
                suffix += ' · готовое/смешанное блюдо'
            lines.append(f"• {row['name'][:85]} — {row['price_rub']:g} ₽{suffix}")
        if not rows:lines.append('Товаров с подтверждённой ценой не получено.')
        lines.append('Кандидаты сохранены отдельно; цены Жорика не перезаписаны.')
        await message.answer('\n'.join(lines)[:3900])
    except Exception:
        log.exception('VkusVill public MCP search failed')
        await message.answer('⚠️ Поиск ВкусВилла недоступен или изменился формат ответа. Действующие цены не затронуты.')
