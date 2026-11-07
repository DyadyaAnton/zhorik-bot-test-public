"""Owner-controlled, privacy-minimized, bounded performance flight recorder."""
import contextvars
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from config import settings

log = logging.getLogger('zhorik.perf05')
_active = False
_entries = deque(maxlen=300)
_trace = contextvars.ContextVar('zhorik_perf_trace', default=None)
_file = settings.data_dir / 'perf05_diagnostics.jsonl'
_MAX_FILE = 500_000


def enabled():
    return _active


def start():
    global _active
    _active = True
    _entries.clear()
    try:
        _file.parent.mkdir(parents=True, exist_ok=True)
        _file.write_text('', encoding='utf-8')
    except OSError:
        log.warning('PERF05 cannot create diagnostic file', exc_info=True)
    return True


def stop():
    global _active
    _active = False


def clear():
    _entries.clear()
    try:
        _file.unlink(missing_ok=True)
    except OSError:
        log.warning('PERF05 cannot remove diagnostic file', exc_info=True)


def add(stage, elapsed):
    if not _active:
        return
    trace = _trace.get()
    if trace is not None:
        trace['stages'][stage] = trace['stages'].get(stage, 0.0) + max(0.0, elapsed)
        trace['counts'][stage] = trace['counts'].get(stage, 0) + 1


def mark(label, elapsed):
    add(label, elapsed)


def begin(label):
    if not _active:
        return None
    data = {'label':label[:90], 'started':time.perf_counter(), 'stages':{}, 'counts':{}}
    return _trace.set(data), data


def end(token_data, error=False):
    if token_data is None:
        return
    token, data = token_data
    try:
        duration = time.perf_counter()-data['started']
        stages = {k:round(v,4) for k,v in data['stages'].items()}
        counts = data['counts']
        entry = {'utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                 'action':data['label'], 'total_s':round(duration,4),
                 'stages_s':stages, 'stage_calls':counts, 'error':bool(error)}
        _entries.append(entry)
        try:
            if _file.exists() and _file.stat().st_size > _MAX_FILE:
                # Recreate from the bounded in-memory ring; do not grow forever.
                _file.write_text(''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in _entries), encoding='utf-8')
            else:
                with _file.open('a',encoding='utf-8') as out:
                    out.write(json.dumps(entry,ensure_ascii=False)+'\n')
        except OSError:
            log.warning('PERF05 cannot write diagnostic file', exc_info=True)
    finally:
        _trace.reset(token)


def report_bytes():
    entries = list(_entries)
    if not entries and _file.exists():
        for line in _file.read_text(encoding='utf-8').splitlines()[-300:]:
            try: entries.append(json.loads(line))
            except (ValueError,TypeError): pass
    lines = ['ЖОРИК PERF05 — отчёт о задержках',
             'Содержимое сообщений, идентификаторы пользователей и токены не записываются.',
             'Время total_s измеряется внутри обработки update и не включает очередь до её начала.',
             'stages_s: sqlite_connect/sqlite_execute/telegram_api/telegram_retry_wait и пр.',
             'Если total_s больше суммы stages_s, остаток — обработчик/планировщик/другая работа.',
             'Запись активна: '+str(_active), 'Записей: '+str(len(entries)), '']
    for x in entries:
        lines.append(f"{x['utc']} | {x['action']} | total={x['total_s']:.3f}s | errors={x['error']}")
        for k,v in sorted(x['stages_s'].items(), key=lambda z:-z[1]):
            lines.append(f"    {k}: {v:.3f}s ({x['stage_calls'].get(k,0)} calls)")
    return ('\n'.join(lines)+'\n').encode('utf-8')


class UpdateTimingMiddleware:
    async def __call__(self, handler, event, data):
        if not _active:
            return await handler(event,data)
        from aiogram.types import Message, CallbackQuery
        if isinstance(event, Message):
            label='message:' + ('command' if (event.text or '').startswith('/') else
                                 'menu_week' if event.text=='🍽 Меню на неделю' else 'text')
        elif isinstance(event, CallbackQuery):
            raw = event.data or ''
            # Limit to action type: never save arbitrary callback arguments.
            bits=raw.split(':')
            label='callback:'+':'.join(bits[:2])[:70]
        else:
            label=type(event).__name__
        token=begin(label)
        error=False
        try:
            return await handler(event,data)
        except Exception:
            error=True
            raise
        finally:
            end(token,error)
