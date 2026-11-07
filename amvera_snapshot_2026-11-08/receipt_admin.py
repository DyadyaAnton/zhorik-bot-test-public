"""Owner-only, read-only-by-default administration for receipt-mail pilot."""
import asyncio
import logging
import time
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, BufferedInputFile

from config import settings
from keyboards import styled
from telegram_retry import safe_call
from jorik_receipt_mail import ReceiptMailError
from jorik_receipts_sync import (list_pending, receipt_stats, review_receipt, sync_once,
                                 pending_receipt_detail, receipt_diagnostics, reparse_legacy_receipts,
                                 ReceiptMailboxChanged)


router = Router(name="receipt_admin")
log = logging.getLogger(__name__)
_scan_lock = asyncio.Lock()


def _owner(uid: int) -> bool:
    return settings.sandbox_owner_id is not None and uid == settings.sandbox_owner_id


def _panel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled("📥 Загрузить всю почту (фон)", "rcpt:scan_all", "success")],
        [styled("📩 Проверить почту (25 писем)", "rcpt:scan")],
        [styled("🧾 Новые чеки", "rcpt:list"), styled("📊 Статистика", "rcpt:stats")],
        [styled("🔬 Диагностика чеков", "rcpt:diagnostics"), styled("📤 Отчёт TXT", "rcpt:export")],
        [styled("🔄 Переразобрать старые чеки (10 писем)", "rcpt:reparse")],
        [styled("⬅️ В админку", "admin:menu_back"), styled("❌ Закрыть", "ui:close", "danger")],
    ])


async def open_panel(c: CallbackQuery):
    if not _owner(c.from_user.id):
        return await c.answer("Только для владельца", show_alert=True)
    await c.answer()
    await safe_call(c.message.edit_text,
                    "🧾 Чеки и цены · тестовый импорт\n"
                    "Почта читается только по запросу.\n"
                    "Подтверждённые чеки попадают в отдельную личную историю.\n"
                    "Общие цены Жорика автоматически не меняются.",
                    reply_markup=_panel_keyboard())


async def _scan_job(message):
    try:
        result = await asyncio.to_thread(sync_once, limit=25)
        body = ("✅ Проверка завершена\n"
                f"Писем просмотрено: {result['mail_scanned']}\n"
                f"Новых чеков: {result['receipts_staged']}\n"
                f"Повторов: {result['duplicates']}\n"
                f"Ошибок: {result['errors']}\n\n"
                "Откройте «Новые чеки» для подтверждения.")
    except ReceiptMailboxChanged:
        body = ("⛔ Яндекс изменил UIDVALIDITY папки. Загрузка остановлена без потери данных. "
                "Не сбрасывайте базу и курсор; требуется безопасная сверка старых писем.")
    except ReceiptMailError:
        body = "⚠️ Проверка почты не удалась. Проверьте IMAP, секреты и постоянную папку /data."
    except Exception:
        log.exception("Receipt mail scan failed")
        body = "⚠️ Неожиданная ошибка импорта. Проверьте логи без вывода секретов."
    await safe_call(message.edit_text, body, reply_markup=_panel_keyboard())


async def _scan_all_job(message):
    """Incremental bounded batches; each completed mail has its own DB checkpoint."""
    totals = {"mail_scanned": 0, "receipts_staged": 0, "duplicates": 0, "errors": 0}
    started = time.monotonic()
    reason = "почта обработана"
    try:
        for batch in range(40):  # hard cap: 2000 emails per manual run
            if time.monotonic() - started > 600:
                reason = "лимит времени 10 минут (можно продолжить повторным запуском)"
                break
            result = await asyncio.to_thread(sync_once, limit=50)
            for key in totals:
                totals[key] += result[key]
            if (batch + 1) % 2 == 0 or result["mail_scanned"] < 50 or result["errors"]:
                await safe_call(message.edit_text,
                    f"📥 Загружаю чеки…\nПисем: {totals['mail_scanned']}\n"
                    f"Чеков: {totals['receipts_staged']}\n"
                    f"Повторов: {totals['duplicates']}\nОшибок: {totals['errors']}")
            if result["errors"]:
                reason = "ошибка письма; повторите загрузку позже"
                break
            if result["mail_scanned"] < 50:
                break
        else:
            reason = "лимит 2000 писем (запустите ещё раз для продолжения)"
        body = (f"✅ Пакетная загрузка: {reason}\n"
                f"Писем просмотрено: {totals['mail_scanned']}\n"
                f"Новых чеков: {totals['receipts_staged']}\n"
                f"Повторов: {totals['duplicates']}\nОшибок: {totals['errors']}\n\n"
                "Данные сохранены по мере обработки. Цены пока не считаются рыночными.")
    except ReceiptMailboxChanged:
        body = (f"⛔ Яндекс изменил UIDVALIDITY. Уже просмотрено писем: {totals['mail_scanned']}. "
                "Импорт остановлен, ранее сохранённые чеки на месте. "
                "Не сбрасывайте курсор автоматически — сообщите разработчику.")
    except Exception:
        log.exception("Receipt batch import failed")
        body = (f"⚠️ Загрузка остановилась. Уже сохранено писем: {totals['mail_scanned']}.\n"
                "Повторный запуск продолжит с контрольной точки. Проверьте логи.")
    await safe_call(message.edit_text, body, reply_markup=_panel_keyboard())


async def _reparse_job(message):
    try:
        result = await asyncio.to_thread(reparse_legacy_receipts, limit=10)
        body = ("🔄 Повторный разбор завершён (курсор новых писем не изменён)\n"
                f"Писем повторно прочитано: {result['emails']}\n"
                f"Записей дополнено: {result['updated']}\n"
                f"Не сопоставлено: {result['missing']}\n"
                f"Неоднозначных совпадений: {result['ambiguous']}\n"
                f"Ошибок: {result['errors']}\n\n"
                "Откройте диагностику для проверки результатов.")
    except ReceiptMailError:
        body = "⚠️ Повторный разбор не выполнен. Проверьте IMAP, UIDVALIDITY и логи."
    except Exception:
        log.exception("Receipt legacy reparse failed")
        body = "⚠️ Ошибка повторного разбора. Проверьте логи."
    await safe_call(message.edit_text, body, reply_markup=_panel_keyboard())


def _diagnostics_txt(data):
    counts = data['counts']
    lines = ["ЖОРИК · ДИАГНОСТИКА ЧЕКОВ · RECEIPTS_02 REV4",
             f"Создано UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             "Без текста писем, ключей доступа, ФИО и адресов.", "",
             f"Ожидают: {counts.get('pending', 0)}",
             f"Сверены: {counts.get('reconciled', 0)}",
             f"Не сверены: {counts.get('unreconciled', 0)}",
             f"Без итога: {counts.get('no_total', 0)}",
             f"Без товаров: {counts.get('no_items', 0)}",
             f"Старые без исходного текста: {counts.get('legacy_no_source', 0)}",
             f"Требуют разбора новой версией: {counts.get('needs_reparse', 0)}",
             "", "ПОСЛЕДНИЕ ДО 500 ЗАПИСЕЙ (без персональных данных)"]
    for row in data['examples']:
        lines.append(f"{row['key']} | сумма={row['total'] or '?'} | позиций={row['items']} | "
                     f"сверено={int(row['ok'])} | исходник={int(row['source_available'])}")
    return '\n'.join(lines)


@router.callback_query(F.data.startswith("rcpt:"))
async def actions(c: CallbackQuery):
    if not _owner(c.from_user.id):
        return await c.answer("Только для владельца", show_alert=True)
    action = (c.data or "").split(":")
    if len(action) < 2:
        return await c.answer("Неизвестное действие")
    if action[1] in ("scan", "scan_all", "reparse"):
        if _scan_lock.locked():
            return await c.answer("Проверка почты уже идёт", show_alert=True)
        # Reserve before first network await, preventing double starts.
        await _scan_lock.acquire()
        try:
            await c.answer("Запускаю проверку")
            await safe_call(c.message.edit_text,
                            "📥 Загружаю всю почту в фоне…" if action[1] == "scan_all" else
                            "📩 Проверяю почту в фоне…" if action[1] == "scan" else
                            "🔄 Повторно читаю старые письма в фоне…")
        except BaseException:
            # No job exists yet, so only this handler owns the lock.
            _scan_lock.release()
            raise
        async def job():
            try:
                if action[1] == "scan":
                    await _scan_job(c.message)
                elif action[1] == "scan_all":
                    await _scan_all_job(c.message)
                else:
                    await _reparse_job(c.message)
            finally:
                _scan_lock.release()
        try:
            asyncio.create_task(job())
        except BaseException:
            _scan_lock.release()
            raise
        return
    await c.answer()
    try:
        if action[1] in ("diagnostics", "export"):
            data = await asyncio.to_thread(receipt_diagnostics)
            if action[1] == "export":
                payload = _diagnostics_txt(data).encode('utf-8-sig')
                return await safe_call(c.message.answer_document,
                    BufferedInputFile(payload, filename="JORIK_RECEIPTS_DIAGNOSTICS_02_REV4.txt"),
                    caption="🔬 Диагностика без текста писем и секретов")
            stats = data['counts']
            text = ("🔬 Диагностика распознавания\n"
                    f"Ожидают: {stats.get('pending', 0)}\n"
                    f"Сверены: {stats.get('reconciled', 0)}\n"
                    f"Не сверены: {stats.get('unreconciled', 0)}\n"
                    f"Без суммы: {stats.get('no_total', 0)}\n"
                    f"Без товаров: {stats.get('no_items', 0)}\n"
                    f"Старые без исходника: {stats.get('legacy_no_source', 0)}\n\n"
                    f"Требуют нового разбора: {stats.get('needs_reparse', 0)}\n\n"
                    "Для подробностей скачайте TXT. Старые письма можно перечитать кнопкой 🔄.")
            return await safe_call(c.message.edit_text, text, reply_markup=_panel_keyboard())
        if action[1] == "stats":
            counts = await asyncio.to_thread(receipt_stats)
            text = ("📊 Чеки\n"
                    f"Ждут проверки: {counts.get('pending_review', 0)}\n"
                    f"Подтверждено: {counts.get('confirmed', 0)}\n"
                    f"Отклонено: {counts.get('rejected', 0)}\n"
                    f"Проблемные письма (ручной разбор): {counts.get('quarantined', 0)}\n"
                    f"Наблюдений цен из чеков: {counts.get('price_observations', 0)}\n"
                    f"Возможные скидки/баллы: {counts.get('benefit_suspected', 0)}\n"
                    "⚠️ Цена в чеке — фактическая, не обязательно обычная цена товара.")
            return await safe_call(c.message.edit_text, text, reply_markup=_panel_keyboard())
        if action[1] == "list":
            offset = 0
            if len(action) >= 3:
                if not action[2].isdigit():
                    return await safe_call(c.message.edit_text, "Некорректная страница",
                                           reply_markup=_panel_keyboard())
                offset = min(int(action[2]), 100000)
            rows = await asyncio.to_thread(list_pending, 6, offset)
            has_next = len(rows) > 5
            rows = rows[:5]
            buttons = [[styled(f"🧾 {row['date'][:16] if row['date'] else 'Без даты'} · {row['total'] or '?'} ₽",
                               "rcpt:view:" + row['key'])] for row in rows]
            pages = []
            if offset:
                pages.append(styled("⬅️ Предыдущие", f"rcpt:list:{max(0, offset-5)}"))
            if has_next:
                pages.append(styled("Следующие ➡️", f"rcpt:list:{offset+5}"))
            if pages:
                buttons.append(pages)
            buttons.append([styled("⬅️ К чекам", "admin:receipts")])
            return await safe_call(c.message.edit_text,
                f"🧾 Чеки на проверку · {offset+1}–{offset+len(rows)}" if rows else "На этой странице чеков нет.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        if action[1] == "view" and len(action) == 3:
            row = await asyncio.to_thread(pending_receipt_detail, action[2])
            if row is None:
                return await safe_call(c.message.edit_text, "Чек не найден или уже обработан.", reply_markup=_panel_keyboard())
            lines = [f"🧾 Чек · {row['total'] or '?'} ₽",
                     f"ID: {row['key']} · письмо UID: {row['uid']}",
                     f"Дата письма: {(row['date'] or 'неизвестна')[:70]}",
                     f"Тема: {(row['subject'] or 'для старых чеков ещё не сохранена')[:100]}",
                     f"Товаров: {len(row['items'])}", ""]
            lines.extend(f"• {i['name'][:75]} — {i['total']} ₽" for i in row['items'][:25])
            if len(row['items']) > 25:
                lines.append(f"…ещё {len(row['items']) - 25} позиций")
            if not row['reconciled']:
                if row['total'] is None:
                    lines.append("⚠️ Итог чека не найден.")
                if not row['items']:
                    lines.append("⚠️ Товары не распознаны.")
                if row['total'] is not None and row['items']:
                    lines.append("⚠️ Арифметика строки или сумма не сходится с итогом.")
                lines.append("Подтверждение заблокировано.")
            buttons = []
            if row['source_text'] is not None:
                buttons.append([styled("🔎 Фрагмент исходного письма", "rcpt:source:" + row['key'])])
            if row['reconciled']:
                buttons.append([styled("✅ Подтвердить", "rcpt:approve:" + row['key'])])
            buttons += [[styled("❌ Отклонить", "rcpt:reject:" + row['key'])],
                        [styled("⬅️ К списку", "rcpt:list")]]
            return await safe_call(c.message.edit_text, "\n".join(lines)[:3700],
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        if action[1] == "source" and len(action) == 3:
            row = await asyncio.to_thread(pending_receipt_detail, action[2])
            if not row:
                return await safe_call(c.message.edit_text, "Чек не найден", reply_markup=_panel_keyboard())
            if row['source_text'] is None:
                return await safe_call(c.message.edit_text, "Исходник пока не сохранён", reply_markup=_panel_keyboard())
            compact = [line.strip() for line in row['source_text'].splitlines() if line.strip(' |\t')]
            return await safe_call(c.message.edit_text,
                "🔎 Исходный текст (возможны персональные данные)\n\n" +
                '\n'.join(compact)[:3000],
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [styled("⬅️ К чеку", "rcpt:view:" + row['key'])]]))
        if action[1] in ("approve", "reject") and len(action) == 3:
            approve = action[1] == "approve"
            verb = "подтвердить" if approve else "отклонить"
            return await safe_call(c.message.edit_text,
                f"Вы точно хотите {verb} этот чек?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [styled("✅ Да, выполнить", f"rcpt:commit:{action[1]}:{action[2]}")],
                    [styled("⬅️ Нет, назад", f"rcpt:view:{action[2]}")],
                ]))
        if action[1] == "commit" and len(action) == 4 and action[2] in ("approve", "reject"):
            result = await asyncio.to_thread(review_receipt, action[3], owner_id=c.from_user.id,
                                             approve=action[2] == "approve")
            return await safe_call(c.message.edit_text, result, reply_markup=_panel_keyboard())
    except ReceiptMailError:
        return await safe_call(c.message.edit_text,
            "⚠️ База чеков пока не готова или недоступна.", reply_markup=_panel_keyboard())
    return await safe_call(c.message.edit_text, "Неизвестное действие.", reply_markup=_panel_keyboard())
