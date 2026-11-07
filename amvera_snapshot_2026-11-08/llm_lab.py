"""Owner-only lab; one active card and no persistent raw prompt/response storage."""
import asyncio
from datetime import datetime, timezone, timedelta
from aiogram.types import InlineKeyboardMarkup, BufferedInputFile
from keyboards import styled
from telegram_retry import safe_call
from llm_usage import stats
from llm_usage_async import reserve_cancellation_safe as _reserve_cancellation_safe
from llm_usage_async import settle_reliably as _settle
from llm_contracts import validate_connection
from llm_prompts import SYSTEM, USER, PROMPT_VERSION

_active = {}  # (owner_id, chat_id) -> message_id; stale cards cannot trigger paid calls
_snapshots = {}  # ephemeral owner-only, no IDs or secret in exported content
_busy = set()
_TTL = 30 * 60


def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('🔌 Проверить ИИ','admin:llm:test'),styled('📊 Токены','admin:llm:tokens')],
        [styled('📤 Запрос','admin:llm:input'),styled('📥 Ответ','admin:llm:output')],
        [styled('🍽 ИИ-анализ последнего меню','admin:llm:review')],
        [styled('📋 Проверка','admin:llm:checks'),styled('📄 Отчёт','admin:llm:report')],
        [styled('⬅️⬅️ Назад','admin:menu_back'),styled('❌ Закрыть','ui:close','danger')]])


def panel_text():
    return '🧠 ИИ-лаборатория · Qwen3-30B\nLLM PILOT 01B\nПроверка подключения и анализ меню только для чтения. Цены и меню не изменяются.'


def mark(c):
    _active[(c.from_user.id, c.message.chat.id)] = c.message.message_id


def is_current(c):
    return _active.get((c.from_user.id,c.message.chat.id)) == c.message.message_id


def _snap(owner):
    for key, old in list(_snapshots.items()):
        if (datetime.now(timezone.utc)-old['at']).total_seconds() >= _TTL:
            _snapshots.pop(key,None)
    item = _snapshots.get(owner)
    if item and (datetime.now(timezone.utc)-item['at']).total_seconds() < _TTL:
        return item
    _snapshots.pop(owner,None)
    return None


def _short(text):
    return text if len(text)<3600 else text[:3590]+'…'


async def _editorial_review(outline, owner):
    """One charged review, strict allergy context, with one settlement."""
    from llm_service import review_menu_json
    from menu_editorial_policy import load_allergies, screen_review, validated_allergies
    allergies = await asyncio.to_thread(load_allergies, owner)
    validated_allergies(allergies)  # reject unknown restrictions before billing
    rid = await _reserve_cancellation_safe()
    if not rid:
        return None, 0
    tokens = None
    try:
        result, tokens = await review_menu_json(outline, allergies=allergies)
        return screen_review(result, allergies)
    finally:
        await asyncio.shield(_settle(rid, tokens))


def _report(owner):
    item = _snap(owner)
    s = stats()
    lines = [panel_text(), 'Период UTC: '+s['period'],
             'Учтено: '+str(s['spent'])+' / '+str(s['limit']),
             'Зарезервировано: '+str(s['reserved']),
             'Вызовов: '+str(s['calls']), 'Неопределённый расход (вызовов без usage): '+str(s['uncertain']), 'Важно: учтено включает оценочные списания; сверяйте фактический расход в Amvera']
    if item:
        lines += ['Статус: '+item['status'],'Модель: '+item.get('model','неизвестна'),
                  'Секунды: '+str(item.get('elapsed','?')),
                  'JSON mode: '+str(item.get('json_mode','?')),
                  'Версия промпта: '+str(item.get('prompt_version','?')),
                  'GET /models: '+(item.get('models') or 'нет данных (см. результаты шлюзов выше)'),
                  'Вход: '+str(item.get('input','unknown')),
                  'Выход: '+str(item.get('output','unknown')),
                  'Всего: '+str(item.get('total','unknown')),
                  'Проверка: '+item.get('validation','не выполнялась'),
                  '\n--- ЗАПРОС ---\n'+item['prompt'],
                  '\n--- ОТВЕТ ---\n'+item['response']]
    return '\n'.join(lines)


async def review_selected_menu(c, menu_id):
    """Review exactly the opened menu; owner authorization and owner-scoped SQL."""
    from config import settings
    if settings.sandbox_owner_id is None or c.from_user.id != settings.sandbox_owner_id:
        return await c.answer('Только для владельца', show_alert=True)
    key=(c.from_user.id,c.message.chat.id)
    if key in _busy:
        return await c.answer('Проверка уже выполняется', show_alert=True)
    _busy.add(key)
    try:
        return await _review_selected_owned(c,menu_id)
    finally:
        _busy.discard(key)


async def _review_selected_owned(c,menu_id):
    from llm_menu_review import menu_outline
    menu = await asyncio.to_thread(menu_outline,c.from_user.id,menu_id)
    if not menu:
        return await c.answer('Меню не найдено или в нём нет блюд', show_alert=True)
    await c.answer('🧠 Анализ запущен')
    try:
        result, rejected = await _editorial_review(menu[1],c.from_user.id)
        if result is None:
            text='⛔ Лимит лаборатории исчерпан. ИИ-запрос не отправлен.'
        else:
            redacted = bool(result.get('editorial_hidden'))
            issues = (['Скрыты из-за пищевых ограничений; отсутствие проблем не подтверждено.']
                      if redacted else result['issues'] or ['Не обнаружены'])
            suggestions = (['Скрыты из-за пищевых ограничений.']
                           if redacted else result['suggestions'] or ['Нет предложений'])
            lines = ['🧠 Анализ Qwen · меню №'+str(menu[0]),
                     'Вывод: '+result['summary'], '⚠️ Возможные проблемы:']
            lines.extend('• '+x for x in issues)
            lines.append('💡 Идеи улучшения:')
            lines.extend('• '+x for x in suggestions)
            if rejected:lines.append(f'🔒 Скрыто текстовых фрагментов ИИ: {rejected}.')
            lines.append('ИИ не подтвердил цены, калорийность и безопасность аллергенов. Меню не изменено.')
            text='\n'.join(lines)
    except Exception as exc:
        text='⚠️ Не удалось проверить меню: '+(str(exc) if type(exc).__name__=='LLMError' else type(exc).__name__)
    from aiogram.types import InlineKeyboardMarkup
    return await safe_call(c.message.answer,_short(text),reply_markup=InlineKeyboardMarkup(
       inline_keyboard=[[styled('❌ Закрыть','ui:close','danger')]]))


async def handle(c, action):
    """Reserve the per-chat paid-operation slot before any await."""
    key=(c.from_user.id,c.message.chat.id)
    if action in ('test','review'):
        if key in _busy:
            return await c.answer('Проверка уже выполняется',show_alert=True)
        _busy.add(key)
        try:
            return await _handle_action(c,action)
        finally:
            _busy.discard(key)
    return await _handle_action(c,action)


async def _handle_action(c, action):
    if action=='open':
        await c.answer()
        mark(c)
        return await safe_call(c.message.edit_text,panel_text(),reply_markup=keyboard())
    if not is_current(c):
        return await c.answer('Карточка устарела. Откройте ИИ-лабораторию заново.',show_alert=True)
    await c.answer()
    if action=='tokens':
        s=await asyncio.to_thread(stats)
        text=(f"📊 Qwen · {s['period']} UTC\nУчтено: {s['spent']:,} / {s['limit']:,}\n"
              f"Резерв: {s['reserved']:,}\nВызовов: {s['calls']}\n"
              f"Без точного usage: {s['uncertain']}\n"
              'Лимит лаборатории — календарный месяц UTC, не дата списания Amvera.\nУчтено = точный usage + консервативные оценки для запросов без usage; не фактическое списание Amvera.')
    elif action in ('input','output','checks'):
        data=_snap(c.from_user.id)
        if not data: text='Нет свежего теста. Запустите «🔌 Проверить ИИ».'
        elif action=='input': text='📤 Обезличенный запрос\n'+data['prompt']
        elif action=='output': text='📥 Ответ\n'+data['response']
        else: text='📋 Проверка\n'+data['validation']+'\nСтатус: '+data['status']
    elif action=='report':
        stamp=datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d_%H-%M-%S_%f')[:-3]
        blob=(await asyncio.to_thread(_report,c.from_user.id)).encode('utf-8')
        return await safe_call(c.message.answer_document,
           BufferedInputFile(blob,filename='ZHORIK_LLM_'+stamp+'_MSK.txt'),caption='🧠 LLM 01A · отчёт')
    elif action=='review':
        from llm_menu_review import latest_menu_outline
        menu = await asyncio.to_thread(latest_menu_outline,c.from_user.id)
        if not menu:
            text='Нет сохранённого недельного меню для анализа. Сначала сгенерируйте и сохраните меню.'
        else:
            key=(c.from_user.id,c.message.chat.id)
            try:
                result, rejected = await _editorial_review(menu[1],c.from_user.id)
                if result is None:
                    text='⛔ Лимит лаборатории исчерпан. ИИ-запрос не отправлен.'
                else:
                    lines=['🍽 ИИ-анализ сохранённого меню №'+str(menu[0]),
                           'Вывод Qwen: '+result['summary'],
                           '⚠️ Возможные проблемы:']
                    redacted=bool(result.get('editorial_hidden'))
                    lines += (['• Скрыты из-за пищевых ограничений; отсутствие проблем не подтверждено.']
                              if redacted else ['• '+x for x in result['issues']] or ['• Не обнаружены'])
                    lines.append('💡 Идеи улучшения:')
                    lines += (['• Скрыты из-за пищевых ограничений.']
                              if redacted else ['• '+x for x in result['suggestions']] or ['• Нет предложений'])
                    if rejected:lines.append(f'🔒 Скрыто текстовых фрагментов ИИ: {rejected}.')
                    lines.append('Это редакторская оценка ИИ. Аллергены, калории, цены и безопасность не подтверждены. Меню не изменено.')
                    text='\n'.join(lines)
            except Exception as exc:
                text='⚠️ Анализ меню не выполнен: '+(str(exc) if type(exc).__name__=='LLMError' else type(exc).__name__)
            finally:
                _busy.discard(key)
    elif action=='test':
        key=(c.from_user.id,c.message.chat.id)
        try:
            rid = await _reserve_cancellation_safe()
        except Exception:
            rid = None
            reserve_error = True
        else:
            reserve_error = False
        if not rid:
            text=('⚠️ Не удалось проверить лимит Qwen из-за ошибки базы данных.'
                  if reserve_error else
                  '⛔ Лимит лаборатории исчерпан. Реальный запрос не отправлен.')
        else:
            settled=False
            try:
                from llm_service import probe
                value = await probe()
                try:
                    validate_connection(value['raw'])
                    validation='JSON: OK; поля и значения: OK'
                    status='Успешно'
                except (ValueError, TypeError) as exc:
                    validation='Ответ не прошёл локальную проверку: '+type(exc).__name__
                    status='Ответ получен, но проверка не пройдена'
                usage=value['usage']
                inp=usage.get('prompt_tokens',usage.get('input_tokens'))
                out=usage.get('completion_tokens',usage.get('output_tokens'))
                tot=usage.get('total_tokens')
                if type(tot) is not int or tot<0:tot=None
                charged, uncertain=await _settle(rid,tot)
                settled=True
                _snapshots[c.from_user.id]={'at':datetime.now(timezone.utc),
                    'prompt':SYSTEM+'\n'+USER, 'response':value['raw'][:12000],
                    'status':status, 'validation':validation, 'prompt_version':PROMPT_VERSION,
                    'model':value['model'],'elapsed':value['elapsed_s'],
                    'json_mode':value['json_mode'],'models':', '.join(value['models'][:15]) or (value.get('models_error') or 'нет данных; подробности по шлюзам указаны выше'), 'input':inp,'output':out,
                    'total':tot if tot is not None else 'нет usage; резерв '+str(charged)}
                text=(f'🔌 Qwen · {status}\nМодель: {value["model"]}\n'
                      f'Время: {value["elapsed_s"]} с\n'
                      f'Токены: {tot if tot is not None else "нет usage (резерв)"}\n'
                      f'JSON mode: {"да" if value["json_mode"] else "нет"}\n'+validation+
                      ('\n⚠️ Алиас отсутствует в /models' if value.get('model_listed') is False else ''))
            except Exception as exc:
                # no raw exception: may contain secrets/provider response
                from llm_service import diagnostic_config
                cfg = diagnostic_config()
                message = str(exc) if type(exc).__name__=='LLMError' else type(exc).__name__
                text=('⚠️ Тест не завершён: '+message+'\n'
                      'API: '+cfg['api']+'\nМодель: '+cfg['model']+'\n'
                      'Ключ: '+cfg['key_status']+'\n'
                      'Время UTC: '+datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
                _snapshots[c.from_user.id]={'at':datetime.now(timezone.utc),
                    'prompt':SYSTEM+'\n'+USER,'response':'Нет ответа','status':'Ошибка',
                    'validation':text,'prompt_version':PROMPT_VERSION}
            finally:
                if not settled:
                    # Also runs for CancelledError (BaseException), unlike the
                    # normal error handler above. Never leave a paid probe's
                    # reservation orphaned after a canceled Telegram update.
                    try:
                        await asyncio.shield(_settle(rid,None))
                    except Exception:
                        pass  # report errors without exposing provider secrets
                _busy.discard(key)
    else:return
    if action=='review':
        # Reports must appear after the current conversation, never replace
        # an older laboratory panel higher up in Telegram history.
        from aiogram.types import InlineKeyboardMarkup
        return await safe_call(c.message.answer,_short(text),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [styled('❌ Закрыть','ui:close','danger')]]))
    return await safe_call(c.message.edit_text,_short(text),reply_markup=keyboard())
