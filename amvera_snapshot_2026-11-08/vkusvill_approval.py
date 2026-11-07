"""Owner-reviewed VkusVill candidate mapping. No automatic promotion or stock claims."""
import re
from decimal import Decimal
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from config import settings
from database import connect_db
from vkusvill_normalize import normalize_candidate

router = Router(name='vkusvill_approval')


def init(db):
    db.execute('''CREATE TABLE IF NOT EXISTS vkusvill_approved_prices (
      owner_id INTEGER NOT NULL, product TEXT NOT NULL COLLATE NOCASE,
      candidate_id TEXT NOT NULL, candidate_name TEXT NOT NULL,
      pack_price REAL NOT NULL, pack_amount REAL NOT NULL, base_unit TEXT NOT NULL,
      observed_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'vkusvill_public_mcp',
      approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(owner_id, product, candidate_id))''')


def preview(db, candidate_id):
    row = db.execute('SELECT * FROM vkusvill_price_candidates WHERE product_id=?', (candidate_id,)).fetchone()
    if not row:
        raise ValueError('Кандидат не найден. Выполните поиск заново.')
    r = dict(row)
    norm = normalize_candidate(r)
    if norm['unit_price_status'] != 'estimated_public_catalog' or norm['base_unit'] not in ('кг', 'л'):
        raise ValueError('Фасовка не определена однозначно; автоматический пересчёт запрещён.')
    from p4_approval_core import _candidate
    from p4_bridge import price_window_days
    _candidate(db, candidate_id, max_age_days=price_window_days())
    return r, norm


def create_pending(db, uid, candidate_id, product):
    r, norm = preview(db, candidate_id)
    db.execute('''CREATE TABLE IF NOT EXISTS vkusvill_approval_pending (
      owner_id INTEGER NOT NULL, candidate_id TEXT NOT NULL,
      product TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(owner_id, candidate_id))''')
    db.execute('INSERT OR REPLACE INTO vkusvill_approval_pending(owner_id,candidate_id,product) VALUES(?,?,?)', (uid,candidate_id,product))
    return r, norm


def approve(db, uid, candidate_id):
    from p4_approval_core import preview_batch, apply_batch, migrate_legacy
    pending = db.execute('SELECT product FROM vkusvill_approval_pending WHERE owner_id=? AND candidate_id=?', (uid,candidate_id)).fetchone()
    if not pending:
        raise ValueError('Подтверждение отсутствует или уже использовано.')
    r, norm = preview(db, candidate_id)
    migrate_legacy(db)
    batch = preview_batch(db, uid, [(pending['product'], candidate_id)], source='telegram')
    if batch['status'] == 'preview':
        apply_batch(db, uid, batch['id'])
    elif batch['status'] not in ('applied','partial'):
        raise ValueError('Партия недоступна для подтверждения')
    db.execute('DELETE FROM vkusvill_approval_pending WHERE owner_id=? AND candidate_id=?',(uid,candidate_id))
    return pending['product'], r, norm


@router.message(Command('vv_candidates'))
async def candidates(m: Message):
    if not m.from_user or m.from_user.id != settings.sandbox_owner_id: return
    with connect_db() as db:
        try:
            rows = db.execute('SELECT product_id,name,price_rub,observed_at FROM vkusvill_price_candidates ORDER BY observed_at DESC LIMIT 20').fetchall()
        except Exception:
            rows=[]
    lines=['🔎 Кандидаты ВкусВилла (последние 20):']
    for r in rows:
        lines.append(f"• ID {r['product_id']} · {r['name'][:65]} · {r['price_rub']:g} ₽")
    lines.append('Для сопоставления: /vv_link ID | название продукта в меню')
    lines.append('Данные публичного каталога; наличие и цена по адресу не проверены.')
    await m.answer('\n'.join(lines)[:3900])


@router.message(Command('vv_link'))
async def link(m: Message):
    if not m.from_user or m.from_user.id != settings.sandbox_owner_id: return
    raw=(m.text or '').partition(' ')[2]
    if '|' not in raw:
        return await m.answer('Формат: /vv_link ID | точное название продукта в меню')
    candidate_id, product = (x.strip() for x in raw.split('|',1))
    if not re.fullmatch(r'[\w-]{1,32}', candidate_id, re.UNICODE) or not 2<=len(product)<=100 or '\n' in product:
        return await m.answer('Некорректный ID или название продукта.')
    try:
        with connect_db() as db:
            r, norm = create_pending(db, m.from_user.id, candidate_id, product)
    except Exception as e:
        return await m.answer('Не удалось подготовить: '+str(e)[:180])
    buttons=InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text='✅ Подтвердить сопоставление',callback_data=f'vvapprove:yes:{candidate_id}')],
      [InlineKeyboardButton(text='❌ Отмена',callback_data=f'vvapprove:no:{candidate_id}')]])
    await m.answer(f"Проверьте вручную:\nТовар меню: {product}\nВкусВилл: {r['name']}\nЦена упаковки: {r['price_rub']:g} ₽\nФасовка: {norm['pack_amount']:g} {norm['base_unit']}\n≈ {norm['price_per_base_unit']:g} ₽/{norm['base_unit']}\n\n⚠️ Публичная ориентировочная цена, наличие по адресу неизвестно. Это подтверждение соответствия товаров, НЕ подтверждение наличия в магазине.", reply_markup=buttons)


@router.callback_query(F.data.startswith('vvapprove:'))
async def confirm(c: CallbackQuery):
    if not c.from_user or c.from_user.id != settings.sandbox_owner_id:
        return await c.answer('Недоступно',show_alert=True)
    parts=(c.data or '').split(':')
    if len(parts)!=3: return await c.answer('Некорректная кнопка',show_alert=True)
    _, action, candidate_id=parts
    try:
        with connect_db() as db:
            if action=='no':
                db.execute('DELETE FROM vkusvill_approval_pending WHERE owner_id=? AND candidate_id=?',(c.from_user.id,candidate_id))
                result='❌ Отменено. Цены не изменены.'
            elif action=='yes':
                product,r,norm=approve(db,c.from_user.id,candidate_id)
                result=f'✅ Сопоставлено: {product} ↔ {r["name"]}. Ориентировочная цена включена в диагностику П4.'
            else: return await c.answer('Некорректное действие',show_alert=True)
    except Exception as e:
        return await c.answer('Ошибка: '+str(e)[:140],show_alert=True)
    await c.answer()
    try: await c.message.edit_text(result)
    except Exception: pass
