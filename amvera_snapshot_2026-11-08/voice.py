import asyncio
import logging
import os
import sqlite3
import secrets
import time
import re
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from access import check_message, check_callback
from config import settings
from database import get_lists, create_list, add_items, get_list, render_list
from keyboards import list_actions_keyboard, MAIN_MENU, main_menu_for, styled
from lists import ListInput, preview_items
from speech import recognize_telegram_voice
from telegram_retry import safe_call
from texts import STALE_BUTTON
from product_parser import normalize_title
from keyboards import title_preview
from voice_intents import parse_voice_intent, describe_intent
from purchase_dictionary import suggest_list
import voice_ai_lab

router = Router(name="voice")
log = logging.getLogger("zhorik.voice")


def _voice_ai_owner(uid):
    return settings.sandbox_owner_id is not None and uid == settings.sandbox_owner_id


def _voice_ai_failure_reason(exc):
    """Only fixed, non-sensitive diagnostic categories reach Telegram."""
    name = type(exc).__name__
    detail = str(exc)
    if name == 'LLMError':
        if detail.startswith('Контракт Qwen: '):
            field = detail.removeprefix('Контракт Qwen: ')
            if field in ('intent', 'items', 'other_tasks', 'корневой объект'):
                return 'Ответ Qwen: некорректное поле «' + field + '»'
        if detail.startswith('HTTP 402'):
            return 'HTTP 402: шлюз отклонил запрос по биллингу'
        if detail.startswith(('HTTP 401', 'HTTP 403')):
            return 'HTTP 401/403: доступ к модели отклонён'
        if detail.startswith('HTTP 429'):
            return 'HTTP 429: ограничение частоты запросов'
        if detail.startswith('HTTP 400'):
            return 'HTTP 400: шлюз отклонил формат запроса'
        if detail.startswith('HTTP 404'):
            return 'HTTP 404: модель или API-маршрут не найден'
        if detail.startswith('HTTP 5'):
            return 'HTTP 5xx: временная ошибка шлюза Qwen'
        if 'Таймаут' in detail:
            return 'Таймаут ответа Qwen'
        if 'Ошибка сети' in detail:
            return 'Ошибка подключения к Qwen'
        if ('JSON' in detail or 'структур' in detail or
                'поле' in detail or 'контракт' in detail or
                'намерение' in detail or 'количество' in detail or
                'позици' in detail or 'задач' in detail or
                'назван' in detail or 'единиц' in detail or
                'ответ' in detail.lower()):
            return 'Ответ Qwen не соответствует формату данных'
        if ('ключ' in detail.lower() or 'секрет' in detail.lower() or
                'api_key' in detail.lower()):
            return 'Не настроен или отклонён ключ Qwen'
        return 'Ошибка Qwen (тип: LLMError)'
    if name == 'RuntimeError':
        if 'Предыдущий тест' in detail:
            return 'Предыдущий тест ещё выполняется'
        if 'лимит токенов' in detail:
            return 'Исчерпан месячный лимит лаборатории'
        if 'выключена' in detail:
            return 'Лаборатория выключена во время запроса'
    return 'Внутренняя ошибка (' + name[:40] + ')'


def _voice_list_title_matches(title, query):
    """Read-only candidate filter, never an automatic target selection."""
    ignore = ('списк', 'готов', 'предыдущ', 'моих', 'моим', 'моего', 'пожалуйста')
    words = [word for word in re.findall(r'\w+', (query or '').casefold())
             if len(word) >= 4 and not any(word.startswith(x) for x in ignore)]
    if not words:
        return True
    label = title.casefold()
    return any(word in label or word[:3] in label for word in words)


def _voice_list_lookup_query(text):
    """Explicit read-only list browsing, even while Qwen is unavailable."""
    raw = (text or '').casefold()
    noun = r'\bспис(?:ок|ка|ку|ком|ки|ков|кам|ками|ках)\b'
    if not re.search(noun, raw):
        return None
    if not re.search(r'\b(?:покажи|показать|найди|найти|выдай|выдать|'
                     r'посмотри|посмотреть|перечень|готовых)\b', raw):
        return None
    match = re.search(noun + r'[^\n]{0,100}?\b(?:на|для)\s+(\w+)', raw)
    return match.group(1) if match else ''


async def _voice_list_buttons(owner_id, query):
    rows = await asyncio.to_thread(get_lists, owner_id)
    matches = [row for row in rows if _voice_list_title_matches(row['title'], query)]
    if not matches and rows:
        matches = rows
    return ([[InlineKeyboardButton(text='📋 ' + row['title'][:45],
                                  callback_data=f"voiceai:list:{row['id']}")]
             for row in matches[:20]], len(matches))


@router.message(Command("voice_ai_lab"))
async def voice_ai_lab_toggle(message: Message, state: FSMContext):
    if not await check_message(message): return
    if not _voice_ai_owner(message.from_user.id): return
    enabled = voice_ai_lab.toggle(message.from_user.id)
    # An earlier list/title wizard must not unexpectedly consume the next voice.
    await state.clear()
    await safe_call(message.answer,
                    "🧪 VOICE AI LAB включена. Наговорите одну фразу.\n"
                    "Покажу, как её понял Жорик и как Qwen. Ничего не сохраню.\n"
                    "Повторите /voice_ai_lab, чтобы выключить." if enabled else
                    "🧪 VOICE AI LAB выключена. Обычный голосовой режим восстановлен.")


async def _run_voice_ai_lab(message: Message, recognized: str):
    # The SpeechKit transcript is an independent owner-only result. Even if
    # Qwen fails, the owner can inspect and forward the exact recognized text.
    query = _voice_list_lookup_query(recognized)
    transcript_buttons = []
    lookup_note = ''
    if query is not None:
        try:
            transcript_buttons, found = await _voice_list_buttons(message.from_user.id, query)
            lookup_note = f'\n\n📋 Найдено списков: {found}. Нажмите название для просмотра.'
        except sqlite3.Error:
            lookup_note = '\n\n⚠️ Не удалось прочитать списки.'
    await safe_call(message.answer,
                    '🎙 Яндекс распознал:\n\n' + recognized[:1600] + lookup_note,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=transcript_buttons + [
                        [styled('❌ Закрыть', 'ui:close', 'danger')]]))
    started = time.monotonic()
    try:
        result = await voice_ai_lab.compare(message.from_user.id, recognized)
    except Exception as exc:
        reason = _voice_ai_failure_reason(exc)
        incident_id = secrets.token_hex(4)
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        # Fixed category only: never log recognized speech, response, or secrets.
        log.warning("VOICE AI LAB failure id=%s category=%s duration_ms=%d",
                    incident_id, reason, elapsed_ms)
        await safe_call(message.answer, '⚠️ Тест не завершён: ' + reason +
                        '\nКод диагностики: ' + incident_id +
                        '\nСписки и меню не изменены.',
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [styled('❌ Закрыть', 'ui:close', 'danger')]]))
        return
    q = result['qwen']
    j = result['jorik']
    products = [f"• {x['name']}" +
                (f" — {x['quantity']} {x['unit'] or 'шт.'}" if x['quantity'] is not None else '')
                for x in q['items'][:15]]
    tokens = result['usage'].get('total_tokens')
    try:
        rate = float(os.getenv('VOICE_AI_EST_RUB_PER_1M', '30'))
        if not 0 <= rate <= 100000:
            rate = 30.0
    except (ValueError, OverflowError):
        rate = 30.0
    cost = (f"\nОценка при условных {rate:g} ₽ / 1 млн токенов: "
            f"{tokens * rate / 1_000_000:.5f} ₽ (не счёт Amvera)"
            if type(tokens) is int and tokens >= 0 else '\nТочный расход токенов не получен.')
    lines = ["🧪 VOICE AI LAB · только для владельца",
             "Яндекс распознал:\n" + recognized[:1100],
             "Жорик: " + j['intent'] + " · уверенность: " + j['confidence'],
             "Qwen: " + q['intent'],
             "Название: " + str(q['list_title'] or 'не определено'),
             "Список назначения: " + str(q['target_list'] or 'не определён'),
             "Товары:\n" + ('\n'.join(products) if products else 'не выделены'),
             "Другие дела: " + (', '.join(q['other_tasks']) if q['other_tasks'] else 'нет'),
             "Уточнение: " + str(q['clarification'] or 'не требуется'),
             "Время Qwen + обработка: " + str(result['elapsed_s']) + " с",
             "Токены (вход / выход / всего): " +
             ' / '.join(str(result['usage'].get(k, '?')) for k in
                        ('prompt_tokens', 'completion_tokens', 'total_tokens')) + cost,
             "Ничего не создано и не сохранено."]
    list_buttons = []
    if q['intent'] == 'FIND_LISTS' and query is None:
        list_buttons, found = await _voice_list_buttons(
            message.from_user.id, q['target_list'] or q['list_title'])
        lines.append('📋 Найденные списки: ' + str(found) +
                     '. Нажмите название для просмотра (без изменений).')
    text = '\n\n'.join(lines)
    if len(text) > 3900:
        text = text[:3820] + '\n… Полный результат — в TXT.'
    keyboard = InlineKeyboardMarkup(inline_keyboard=list_buttons + [
        [InlineKeyboardButton(text='📄 Скачать сравнение TXT',
                              callback_data='voiceai:report:' + result['report_id'])],
        [styled('❌ Закрыть', 'ui:close', 'danger')]])
    await safe_call(message.answer, text, reply_markup=keyboard)


@router.callback_query(F.data.startswith('voiceai:list:'))
async def voice_ai_view_list(callback: CallbackQuery):
    if not await check_callback(callback): return
    if not _voice_ai_owner(callback.from_user.id): return
    raw_id = callback.data.rsplit(':', 1)[-1]
    if not raw_id.isdigit():
        return await callback.answer('Неверный список', show_alert=True)
    listing = await asyncio.to_thread(render_list, callback.from_user.id, int(raw_id))
    if not listing:
        return await callback.answer('Список не найден', show_alert=True)
    await callback.answer()
    return await safe_call(callback.message.answer, listing[:3900],
                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                               [styled('❌ Закрыть', 'ui:close', 'danger')]]))


@router.callback_query(F.data.startswith('voiceai:report:'))
async def voice_ai_report(callback: CallbackQuery):
    if not await check_callback(callback): return
    if not _voice_ai_owner(callback.from_user.id): return
    item = voice_ai_lab.latest(callback.from_user.id, callback.data.rsplit(':', 1)[-1])
    if not item:
        await callback.answer('Отчёт устарел. Сделайте новый тест.', show_alert=True)
        return
    await callback.answer()
    name = 'ZHORIK_VOICE_AI_' + item['created_at'][:19].replace(':', '-') + '.txt'
    await safe_call(callback.message.answer_document,
                    BufferedInputFile(voice_ai_lab.report_text(item).encode('utf-8'), filename=name),
                    caption='🔬 Голос: сравнение Жорик / Qwen · содержит личный текст')

class VoiceEdit(StatesGroup):
    waiting_text = State()
    waiting_title = State()


async def preview_voice_purchases(message, state, raw):
    """Conservative synonym hints; existing preview remains the save gate."""
    try:
        suggested, changes = suggest_list(raw)
    except (OSError, ValueError, sqlite3.Error):
        log.warning("Справочник покупок недоступен; сохраняем исходную фразу")
        suggested, changes = raw, []
    if changes:
        lines = [f"• {before} → {after} ({category})" for before, after, category in changes]
        await safe_call(message.answer,
                        "📚 Справочник покупок понял так:\n" + "\n".join(lines[:15]) +
                        "\nЕсли неверно — исправьте перед сохранением.")
        # lists.split_items treats line-separated entries literally. Its
        # older speech lexer does not yet know every dictionary unit/alias.
        # A trailing newline keeps a single confirmed suggestion intact.
        if not any(separator in suggested for separator in (",", ";", "\n")):
            suggested += "\n"
    await preview_items(message, state, suggested)

def voice_keyboard(token, intent=None):
    buttons = [
        [InlineKeyboardButton(text="➕ Новый список из пунктов", callback_data=f"voice:new:{token}")],
        [InlineKeyboardButton(text="📋 Добавить в существующий", callback_data=f"voice:existing:{token}")],
        [InlineKeyboardButton(text="✏️ Исправить текст", callback_data=f"voice:edit:{token}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"voice:cancel:{token}")],
        [styled('❌ Закрыть', f'voice:close:{token}', 'danger')],
    ]
    if intent and (intent.intent not in ("LIST_CREATE", "LIST_ADD") or
                   (intent.intent == "LIST_ADD" and not intent.shopping_text)):
        buttons = buttons[2:]
    elif intent and intent.intent == "LIST_ADD":
        # Never infer a target from get_lists() ordering: the user must
        # explicitly select the list by its displayed title.
        buttons = buttons[1:]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def preview_voice(message, state, recognized):
    token = secrets.token_hex(8)
    intent = parse_voice_intent(recognized)
    await state.update_data(recognized_text=recognized, voice_token=token,
                            shopping_text=intent.shopping_text,
                            voice_intent=intent.intent)
    label = describe_intent(intent)
    note = ("\n⚠️ Для меню пока распознано только намерение; "
            "генерацию в этом патче не запускаем." if intent.intent.startswith("MENU_") else
            "\nУточните формулировку перед добавлением." if
            intent.intent == "LIST_ADD" and not intent.shopping_text else
            "\nПроверьте состав пунктов перед сохранением.")
    await safe_call(message.answer,
                    f"Распознано:\n\n{recognized}\n\nПохоже, вы хотите: {label}.{note}",
                    reply_markup=voice_keyboard(token, intent))

@router.message(F.voice)
async def voice_message(message: Message, state: FSMContext, bot: Bot):
    if not await check_message(message): return
    if not settings.yandex_api_key:
        await safe_call(message.answer, "Для голосового ввода задайте YANDEX_API_KEY.")
        return
    current = await state.get_state()
    await safe_call(message.answer, "Распознаю голосовое сообщение…")
    try:
        file_info = await bot.get_file(message.voice.file_id)
        audio_file = await bot.download_file(file_info.file_path)
        recognized = await asyncio.to_thread(recognize_telegram_voice, audio_file.read())
    except Exception as exc:
        log.warning("Ошибка распознавания: %s", type(exc).__name__)
        await safe_call(message.answer, str(exc) if isinstance(exc, RuntimeError) else "Не удалось распознать голос.")
        return
    if _voice_ai_owner(message.from_user.id) and voice_ai_lab.is_enabled(message.from_user.id):
        await _run_voice_ai_lab(message, recognized)
        return
    from user_preferences import PreferenceInput,parsed_preference
    if current == PreferenceInput.value.state:
        from user_preferences import save as preference_text_save
        # Reuse exactly the same parsing and confirmation logic as typed text.
        from types import SimpleNamespace
        proxy=SimpleNamespace(text=recognized,from_user=message.from_user,answer=message.answer)
        await preference_text_save(proxy,state)
        return
    if current == ListInput.name.state:
        if not 1 <= len(recognized) <= 100:
            await safe_call(message.answer, "Название получилось слишком длинным. Повторите.")
            return
        title=normalize_title(recognized)
        token = secrets.token_hex(8)
        await state.update_data(pending_title=title, pending_title_token=token)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать список", callback_data=f"vtitle:save:{token}")],
            [InlineKeyboardButton(text="✏️ Исправить", callback_data=f"vtitle:edit:{token}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"vtitle:cancel:{token}")],
        ])
        await safe_call(message.answer, f"📋 Создать список «{title}»?", reply_markup=title_preview(token))
        return
    if current in (ListInput.items.state, ListInput.edit_draft.state):
        await preview_voice_purchases(message, state, recognized)
        return
    await state.clear()
    await preview_voice(message, state, recognized)

@router.callback_query(F.data.startswith("vtitle:"))
async def voice_title_action(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    parts = callback.data.split(":")
    data = await state.get_data()
    if len(parts)!=3 or data.get("pending_title_token") != parts[2]:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    if parts[1] == "save":
        title = data["pending_title"]
        list_id = create_list(callback.from_user.id, title)
        await state.clear()
        await safe_call(callback.message.answer, f"✅ Список «{title}» создан.\n\n{render_list(callback.from_user.id,list_id)}", reply_markup=list_actions_keyboard(list_id))
    elif parts[1] == "edit":
        await state.update_data(pending_title_token=None)
        await state.set_state(ListInput.name)
        await safe_call(callback.message.answer, "Отправьте правильное название текстом или голосом.")
    elif parts[1] == "cancel":
        await state.clear()
        await safe_call(callback.message.answer, "Создание отменено.", reply_markup=main_menu_for(callback.from_user.id))
    else:
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    await callback.answer()

@router.callback_query(F.data.startswith(("voice:new:", "voice:existing:", "voice:edit:", "voice:cancel:", "voice:close:")))
async def voice_action(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    parts = callback.data.split(":")
    data = await state.get_data()
    if len(parts)!=3 or data.get("voice_token") != parts[2] or not data.get("recognized_text"):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    action, token = parts[1], parts[2]
    intent_name = data.get("voice_intent")
    if (action == "new" and intent_name != "LIST_CREATE") or (
            action == "existing" and intent_name not in ("LIST_CREATE", "LIST_ADD")):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    if action in ("new", "existing") and intent_name == "LIST_ADD" and not data.get("shopping_text"):
        await callback.answer("Сначала уточните состав покупок.", show_alert=True)
        return
    if action == 'close':
        await state.clear()
        await safe_call(callback.message.delete)
    elif action == "cancel":
        await state.clear()
        await safe_call(callback.message.answer, "Не сохраняю.", reply_markup=main_menu_for(callback.from_user.id))
    elif action == "edit":
        await state.update_data(voice_token=None)
        await state.set_state(VoiceEdit.waiting_text)
        await safe_call(callback.message.answer, "Отправьте исправленный текст.")
    elif action == "new":
        await state.update_data(voice_token=None)
        await state.set_state(VoiceEdit.waiting_title)
        await safe_call(callback.message.answer, "Как назвать новый список? Отправьте название текстом.")
    elif action == "existing":
        rows = get_lists(callback.from_user.id)
        if not rows:
            await safe_call(callback.message.answer, "Списков нет. Создайте список через «➕ Новый список».")
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=row["title"][:45], callback_data=f"voice:save:{token}:{row['id']}")]
                for row in rows[:50]])
            await safe_call(callback.message.answer, "Выберите список для добавления:", reply_markup=kb)
    await callback.answer()

@router.message(VoiceEdit.waiting_title, F.text)
async def voice_new_title(message: Message, state: FSMContext):
    if not await check_message(message): return
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 100:
        await safe_call(message.answer, "Название должно быть от 1 до 100 символов.")
        return
    data = await state.get_data()
    list_id = create_list(message.from_user.id, title)
    await state.update_data(target_list_id=list_id)
    await preview_voice_purchases(message, state, data.get("shopping_text") or data["recognized_text"])

@router.callback_query(F.data.startswith("voice:save:"))
async def voice_save(callback: CallbackQuery, state: FSMContext):
    if not await check_callback(callback): return
    parts = callback.data.split(":")
    data = await state.get_data()
    if (len(parts)!=4 or data.get("voice_token")!=parts[2] or
            not parts[3].isdigit() or not data.get("recognized_text") or
            data.get("voice_intent") not in ("LIST_CREATE", "LIST_ADD") or
            (data.get("voice_intent") == "LIST_ADD" and not data.get("shopping_text"))):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    list_id = int(parts[3])
    if not get_list(callback.from_user.id, list_id):
        await callback.answer(STALE_BUTTON, show_alert=True)
        return
    await state.update_data(target_list_id=list_id)
    await preview_voice_purchases(callback.message, state, data.get("shopping_text") or data["recognized_text"])
    await callback.answer()

@router.message(VoiceEdit.waiting_text, F.text)
async def voice_edit(message: Message, state: FSMContext):
    if not await check_message(message): return
    await state.set_state(None)
    await preview_voice(message, state, message.text.strip())

