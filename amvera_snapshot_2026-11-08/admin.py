import asyncio
import logging
from datetime import datetime, timezone
from aiogram import Router, F
from telegram_retry import safe_call
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message,InlineKeyboardMarkup,BufferedInputFile
from keyboards import styled
from access import allowed
from config import settings
from database import allow_user, deny_user, list_allowed_users
from menu_mode import get_mode,set_mode

router = Router(name="admin")
_recipe_import_busy = set()  # Accessed only from the asyncio event loop.


class RecipeTxtStates(StatesGroup):
    waiting_file = State()
    waiting_confirm = State()


def _recipe_txt_worker_finished(uid, task):
    _recipe_import_busy.discard(uid)
    try:
        failure = task.exception()
    except asyncio.CancelledError:
        failure = asyncio.CancelledError()
    if failure is not None:
        logging.getLogger('zhorik.recipes').warning(
            'Recipe TXT worker failed category=%s', type(failure).__name__)


def _admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('🗄 Управление ценами','admin:prices')],
        [styled('🧾 Чеки и цены','admin:receipts')],
        [styled('📨 Обращения','admin:feedback')],
        [styled('🔬 Диагностика скорости','admin:perf')],
        [styled('🧠 ИИ-лаборатория','admin:llm:open')],
        [styled('🎙 Голос · Жорик + Qwen','admin:voiceai')],
        [styled('🔎 Qwen · гипотезы цен','admin:aiprice:open')],
        [styled('📚 Каталог рецептов · диагностика','admin:recipes:open')],
        [styled('📥 Пополнить рецепты из TXT','admin:recipes:txt')],
        [styled('📅 Проверка сохранённых меню','admin:menus:audit:0')],
        [styled('⬅️⬅️ Главное меню','admin:home','primary'),styled('❌ Закрыть','ui:close','danger')],
    ])


def _is_owner(user_id: int) -> bool:
    return settings.sandbox_owner_id is not None and user_id == settings.sandbox_owner_id


@router.message(Command("admin_allow"))
async def admin_allow(message: Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        await safe_call(message.answer, "Команда доступна только владельцу песочницы.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        await safe_call(message.answer, "Использование: /admin_allow <telegram_id>")
        return
    user_id = int(parts[1])
    allow_user(user_id, message.from_user.id)
    await safe_call(message.answer, f"Пользователю {user_id} разрешён доступ.")


@router.message(Command("admin_deny"))
async def admin_deny(message: Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        await safe_call(message.answer, "Команда доступна только владельцу песочницы.")
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        await safe_call(message.answer, "Использование: /admin_deny <telegram_id>")
        return
    user_id = int(parts[1])
    if user_id == settings.sandbox_owner_id:
        await safe_call(message.answer, "Нельзя удалить владельца из доступа.")
        return
    removed = deny_user(user_id)
    await safe_call(message.answer, "Доступ отозван." if removed else "Пользователь не был в белом списке.")


@router.message(Command("admin_users"))
async def admin_users(message: Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        await safe_call(message.answer, "Команда доступна только владельцу песочницы.")
        return
    rows = list_allowed_users()
    if not rows:
        await safe_call(message.answer, "Белый список пуст.")
        return
    lines = [f"• {row['telegram_id']} (добавил {row['added_by']})" for row in rows]
    await safe_call(message.answer, "Разрешённые пользователи:\n" + "\n".join(lines))


@router.message(Command("admin_update_prices"))
async def admin_update_prices(message: Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        await safe_call(message.answer, "Команда доступна только владельцу песочницы.")
        return
    from price07_ui import all_refresh
    await all_refresh(message)


@router.message(Command("perf"))
async def perf_command(message: Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return await safe_call(message.answer, "Только для владельца.")
    from perf_diagnostics import start,stop,clear,report_bytes,enabled
    args=(message.text or '').split(maxsplit=1)
    action=args[1].strip().lower() if len(args)>1 else 'panel'
    if action=='start':
        start(); return await safe_call(message.answer,'🔬 Запись задержек включена. Поработайте с кнопками, затем /perf report.')
    if action=='stop':
        stop(); return await safe_call(message.answer,'⏹ Запись остановлена. /perf report — скачать TXT.')
    if action=='clear':
        clear(); return await safe_call(message.answer,'🧹 Удалены только диагностические записи.')
    if action=='report':
        return await safe_call(message.answer_document,BufferedInputFile(report_bytes(),filename=_perf_filename()),caption='🔬 Диагностика скорости')
    return await safe_call(message.answer,'🔬 Диагностика скорости\nСтатус: '+('запись идёт' if enabled() else 'остановлена')+'\nНажмите кнопку ниже, затем работайте с Жориком, после скачайте TXT.',reply_markup=_perf_keyboard())


def _perf_filename():
    return 'ZHORIK_PERF05_'+datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S_%f')[:-3]+'_UTC.txt'


def _perf_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('▶️ Начать запись','admin:perf:start'),styled('⏹ Остановить','admin:perf:stop')],
        [styled('📤 Скачать TXT-отчёт','admin:perf:report')],
        [styled('🧹 Очистить диагностику','admin:perf:clear')],
        [styled('⬅️⬅️ Назад','admin:menu_back'),styled('❌ Закрыть','ui:close','danger')]])


@router.message(Command("admin"))
@router.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        await safe_call(message.answer, "Только для владельца.")
        return
    await safe_call(message.answer, "🔐 Админ-кабинет\nЦены и резервирование: /admin_prices\nОбращения: /admin_feedback\nБелый список: /admin_users\nРазрешить: /admin_allow ID\nЗапретить: /admin_deny ID",reply_markup=_admin_keyboard())


@router.message(RecipeTxtStates.waiting_file, F.document)
async def recipe_txt_document(message: Message, state: FSMContext):
    if not message.from_user or not _is_owner(message.from_user.id):
        return await safe_call(message.answer, 'Только для владельца.')
    from io import BytesIO
    from recipe_txt_import import MAX_BYTES, parse_txt
    doc = message.document
    if (doc.file_size is not None and doc.file_size > MAX_BYTES) or not (doc.file_name or '').lower().endswith('.txt'):
        return await safe_call(message.answer, 'Нужен TXT в UTF-8, не более 3 МБ.')
    try:
        stream = BytesIO()
        await message.bot.download(doc, destination=stream)
        payload = stream.getvalue()
        if len(payload) > MAX_BYTES:
            raise ValueError('TXT превышает лимит')
        parsed = await asyncio.to_thread(parse_txt, payload)
    except Exception as exc:
        logging.getLogger('zhorik.recipes').warning('Recipe TXT rejected category=%s', type(exc).__name__)
        return await safe_call(message.answer, '⚠️ TXT не прошёл проверку. Ничего не добавлено.')
    await state.update_data(recipe_txt_payload=payload.decode('utf-8-sig'))
    await state.set_state(RecipeTxtStates.waiting_confirm)
    return await safe_call(message.answer,
        f'📚 Проверено записей: {len(parsed)}. Новые рецепты останутся кандидатами до проверки. Добавить?',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [styled('✅ Импортировать TXT','admin:recipes:txt_confirm','success')],
            [styled('❌ Отмена','admin:recipes:txt_cancel','danger')]]))

from aiogram.types import CallbackQuery
@router.callback_query(F.data.startswith('admin:'))
async def admin_navigation(c:CallbackQuery, state: FSMContext):
    if not _is_owner(c.from_user.id):return await c.answer('Только владелец',show_alert=True)
    if c.data.startswith('admin:menus:audit:'):
        from menu_history_audit import list_menus, audit_menu, format_audit, format_saved_menu
        parts=c.data.split(':')
        if (len(parts)==6 and parts[3]=='show' and
                parts[4].isdigit() and parts[5].isdigit()):
            await c.answer()
            chunks=format_saved_menu(c.from_user.id,int(parts[5]))
            if not chunks:
                return await safe_call(c.message.answer,'Меню не найдено или повреждено.')
            for chunk in chunks:
                await safe_call(c.message.answer,chunk)
            return
        if len(parts) not in (4,5) or not all(x.isdigit() for x in parts[3:]):
            return await c.answer('Неверный запрос',show_alert=True)
        if len(parts)==5:
            await c.answer()
            menu_id=int(parts[4])
            checked=audit_menu(c.from_user.id,menu_id)
            if checked is None:
                return await safe_call(c.message.answer,'Меню не найдено или формат не поддерживается.',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [styled('❌ Закрыть','ui:close','danger')]]))
            return await safe_call(c.message.answer,format_audit(checked),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [styled('🍽 Показать все блюда и граммовки',f'admin:menus:audit:show:{parts[3]}:{menu_id}')],
                    [styled('❌ Закрыть','ui:close','danger')]]))
        try:
            page=int(parts[3])
        except (ValueError, OverflowError):
            return await c.answer('Неверная страница',show_alert=True)
        if not 0 <= page <= 100000:
            return await c.answer('Неверная страница',show_alert=True)
        await c.answer()
        total,ids=list_menus(c.from_user.id,page=page)
        buttons=[[styled(f'🍽 Меню №{menu_id}',f'admin:menus:audit:{page}:{menu_id}')]
                 for menu_id in ids]
        navigation=[]
        if page:navigation.append(styled('⬅️⬅️',f'admin:menus:audit:{page-1}'))
        if (page+1)*8<total:navigation.append(styled('➡️➡️',f'admin:menus:audit:{page+1}'))
        if navigation:buttons.append(navigation)
        buttons.append([styled('⬅️⬅️ В админку','admin:menu_back'),
                        styled('❌ Закрыть','ui:close','danger')])
        return await safe_call(c.message.edit_text,
            f'📅 История меню · {total} записей\nПроверка только для чтения. Выберите меню.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    if c.data.startswith('admin:recipes:'):
        action = c.data.rsplit(':', 1)[-1]
        if action == 'txt':
            await state.set_state(RecipeTxtStates.waiting_file)
            await c.answer()
            return await safe_call(c.message.edit_text,
                '📥 Пришлите TXT-файл рецептов документом. Формат JORIK_RECIPE_CANDIDATES_V1.\n'
                'Записи сохраняются только как кандидаты. Изменения — после подтверждения.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [styled('❌ Отмена','admin:recipes:txt_cancel','danger')]]))
        if action == 'txt_cancel':
            await state.clear()
            await c.answer('Отменено')
            return await safe_call(c.message.edit_text, 'Импорт TXT отменён. Данные не изменены.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [styled('⬅️⬅️ В админку','admin:menu_back')],
                    [styled('❌ Закрыть','ui:close','danger')]]))
        if action == 'txt_confirm':
            from recipe_txt_import import import_txt
            if await state.get_state() != RecipeTxtStates.waiting_confirm.state:
                return await c.answer('Подтверждение устарело',show_alert=True)
            if c.from_user.id in _recipe_import_busy:
                return await c.answer('Импорт уже выполняется',show_alert=True)
            uid = c.from_user.id
            _recipe_import_busy.add(uid)
            task = None
            try:
                await c.answer('Проверяю и добавляю')
                payload = (await state.get_data()).get('recipe_txt_payload')
                if type(payload) is not str:
                    raise ValueError('Нет ожидающего файла')
                task = asyncio.create_task(asyncio.to_thread(import_txt, payload.encode('utf-8')))
                # Cancellation of the Telegram handler does not cancel SQLite
                # work. Keep the busy lock until the actual worker completes.
                task.add_done_callback(lambda finished: _recipe_txt_worker_finished(uid, finished))
                result = await asyncio.shield(task)
                msg = (f'✅ TXT обработан: {result["received"]}.\n'
                       f'Новых кандидатов: {result["added"]}.\n'
                       f'Уже существовали: {result["duplicates"]}.\n'
                       'Проверенные рецепты, цены и меню не изменены.')
            except asyncio.CancelledError:
                logging.getLogger('zhorik.recipes').warning('Recipe TXT handler cancelled; worker may still finish')
                raise
            except Exception as exc:
                logging.getLogger('zhorik.recipes').warning('Recipe TXT import failed category=%s', type(exc).__name__)
                msg = ('⚠️ Не удалось подтвердить итог TXT. Возможны изменения в каталоге. '
                       'Повторно откройте импорт: уже добавленные записи не дублируются.')
            finally:
                if task is None:
                    _recipe_import_busy.discard(uid)
                await state.clear()
            return await safe_call(c.message.edit_text, msg,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [styled('⬅️⬅️ В админку','admin:menu_back')],
                    [styled('❌ Закрыть','ui:close','danger')]]))
        if action == 'open':
            from recipe_import_job import inspect_source
            try:
                counts = await asyncio.to_thread(inspect_source)
                message = ('📚 Импорт рецептов · только владелец\n'
                           'В импортной базе:\n'
                           f'• блюда меню: {counts["recipe_source_dishes"]}\n'
                           f'• OCR-кандидаты: {counts["recipe_ocr_candidates"]}\n'
                           f'• Povar.ru: {counts["recipe_web_candidates"]}\n\n'
                           'Импорт создаст резервную копию существующей базы.\n'
                           'Списки, меню и цены не меняются.')
                buttons = [[styled('✅ Подтвердить импорт', 'admin:recipes:confirm', 'success')],
                           [styled('⬅️⬅️ Назад','admin:menu_back'), styled('❌ Закрыть','ui:close','danger')]]
            except Exception as exc:
                logging.getLogger('zhorik.recipes').warning('Recipe source inspect failed category=%s', type(exc).__name__)
                message = '⚠️ Импортная база недоступна или не прошла проверку. Данные не изменены.'
                buttons = [[styled('⬅️⬅️ Назад','admin:menu_back'), styled('❌ Закрыть','ui:close','danger')]]
            await c.answer()
            return await safe_call(c.message.edit_text, message,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        if action == 'confirm':
            uid = c.from_user.id
            if uid in _recipe_import_busy:
                return await c.answer('Импорт уже выполняется', show_alert=True)
            _recipe_import_busy.add(uid)
            try:
                await c.answer('Импорт начат')
                from recipe_import_job import run_import
                result = await asyncio.to_thread(run_import)
                added = result['added']
                msg = ('✅ Импорт рецептов завершён.\n'
                       f'Меню: +{added["recipe_source_dishes"]}\n'
                       f'OCR: +{added["recipe_ocr_candidates"]}\n'
                       f'Povar.ru: +{added["recipe_web_candidates"]}\n'
                       + ('Рабочая база создана впервые.\n' if result['created_new'] else
                          'Резервная копия создана в /data/recipe_catalog_backups.\n')
                       + 'Списки, меню и цены не изменены.')
            except Exception as exc:
                logging.getLogger('zhorik.recipes').warning('Recipe import failed category=%s', type(exc).__name__)
                if type(exc).__name__ == 'AtomicPublishUnsupported':
                    msg = ('⚠️ Импорт не завершён: хранилище Amvera не поддерживает безопасное создание базы. '
                           'Данные не перезаписаны. Передайте этот экран разработчику.')
                else:
                    msg = '⚠️ Импорт не завершён. Проверьте файлы и сообщите разработчику. Резервные копии не удалялись.'
            finally:
                _recipe_import_busy.discard(uid)
            return await safe_call(c.message.edit_text, msg,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                       [styled('⬅️⬅️ В админку','admin:menu_back'), styled('❌ Закрыть','ui:close','danger')]]))
        return await c.answer('Неизвестное действие', show_alert=True)
    if c.data.startswith('admin:llm:'):
        from llm_lab import handle
        return await handle(c, c.data.rsplit(':', 1)[-1])
    if c.data.startswith('admin:aiprice:'):
        from ai_price_lab import handle
        return await handle(c, c.data[len('admin:aiprice:'):])
    if c.data=='admin:voiceai':
        import voice_ai_lab
        enabled=voice_ai_lab.toggle(c.from_user.id)
        # Match /voice_ai_lab: an old list wizard must not capture later text.
        await state.clear()
        await c.answer('Голосовая лаборатория включена' if enabled else 'Голосовая лаборатория выключена')
        return await safe_call(c.message.edit_text,
            '🎙 Голосовая лаборатория: '+('ВКЛЮЧЕНА' if enabled else 'ВЫКЛЮЧЕНА')+
            '\nВключённый режим перехватывает голос владельца и не записывает списки или меню.'+
            '\nНажмите кнопку повторно для переключения.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [styled('⏻ Включить / выключить','admin:voiceai')],
                [styled('⬅️⬅️ Назад','admin:menu_back'),styled('❌ Закрыть','ui:close','danger')]]))
    if c.data=='admin:receipts':
        from receipt_admin import open_panel
        return await open_panel(c)
    if c.data=='admin:perf' or c.data.startswith('admin:perf:'):
        from perf_diagnostics import start,stop,clear,report_bytes,enabled
        action=c.data.split(':')[-1]
        await c.answer()
        if action=='start':start()
        elif action=='stop':stop()
        elif action=='clear':clear()
        elif action=='report':
            return await safe_call(c.message.answer_document,BufferedInputFile(report_bytes(),filename=_perf_filename()),caption='🔬 PERF05: отчёт о задержках')
        return await safe_call(c.message.edit_text,'🔬 Диагностика скорости\nСтатус: '+('▶️ запись идёт' if enabled() else '⏹ остановлена')+'\nНажимайте обычные кнопки, затем скачайте отчёт.',reply_markup=_perf_keyboard())
    if c.data=='admin:home':
        from home_ui import home_keyboard
        await safe_call(c.message.edit_text,'🐾 Главное меню Жорика',reply_markup=home_keyboard())
    elif c.data=='admin:prices':
        from admin_prices import keyboard
        await safe_call(c.message.edit_text,'🔐 Управление ценами',reply_markup=keyboard())
    elif c.data=='admin:menu_mode' or c.data.startswith('admin:menu_set:'):
        if c.data.startswith('admin:menu_set:'):
            set_mode(c.data.split(':')[-1])
        current=get_mode()
        labels={'local':'🧮 Автономный','deepseek':'🧠 DeepSeek API','openai':'✨ OpenAI API','amvera':'🧠 Qwen (Amvera)'}
        buttons=[[styled(('☑️ ' if current==mode else '')+label,'admin:menu_set:'+mode)] for mode,label in labels.items()]
        buttons.append([styled('⬅️ В админку','admin:menu_back')])
        await safe_call(c.message.edit_text,'🍽 Режим генерации меню\nВыбор действует для всех пользователей.\nТекущий: '+labels[current]+'\n\nДля ИИ требуется настроенный API.',reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    elif c.data=='admin:menu_back':
        await state.clear()
        await safe_call(c.message.edit_text,'🔐 Админ-кабинет',reply_markup=_admin_keyboard())
    elif c.data=='admin:feedback':
        await safe_call(c.message.answer,'📨 Обращения пользователей: /admin_feedback')
    await c.answer()
