"""Durable per-chat registry of bot inline cards and best-effort /clean.

Request middleware observes successful Telegram API calls. Database failures
never suppress the actual Telegram reply. No user messages are deleted.
"""
import logging
import asyncio
import time

from aiogram.methods import (SendMessage, EditMessageText, DeleteMessage,
                             SendDocument, SendPhoto, SendVideo, SendVoice,
                             EditMessageCaption, EditMessageReplyMarkup)
from aiogram.types import Message, InlineKeyboardMarkup
from database import connect_db
from keyboards import styled

log=logging.getLogger(__name__)
MAX_CARDS_PER_CHAT=150
_pending_registry=set()

def _schedule_registry(action, chat_id, message_id):
    """Best-effort tracking must not delay Telegram API responses."""
    async def worker():
        started=time.perf_counter()
        try:
            await asyncio.to_thread(record if action == "record" else forget, chat_id, message_id)
        except Exception:
            log.warning("Cannot update card registry", exc_info=True)
        elapsed=time.perf_counter()-started
        if elapsed >= 0.5:
            log.warning("PERF03 card_registry %s took %.3fs", action, elapsed)
    task=asyncio.create_task(worker())
    _pending_registry.add(task)
    task.add_done_callback(_pending_registry.discard)



def init():
    with connect_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS zui_cards (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(chat_id,message_id))''')


def record(chat_id,message_id):
    with connect_db() as db:
        db.execute('INSERT OR IGNORE INTO zui_cards(chat_id,message_id) VALUES(?,?)',(chat_id,message_id))
        db.execute('''DELETE FROM zui_cards WHERE chat_id=? AND message_id NOT IN (
            SELECT message_id FROM zui_cards WHERE chat_id=? ORDER BY message_id DESC LIMIT ?)''',
            (chat_id,chat_id,MAX_CARDS_PER_CHAT))


def forget(chat_id,message_id):
    with connect_db() as db:
        db.execute('DELETE FROM zui_cards WHERE chat_id=? AND message_id=?',(chat_id,message_id))


def tracked(chat_id):
    with connect_db() as db:
        return [row[0] for row in db.execute(
            'SELECT message_id FROM zui_cards WHERE chat_id=? ORDER BY message_id DESC',(chat_id,))]


def _private_numeric_chat(chat_id):
    # Only private Telegram user chats are allowed by /clean. Channel/group
    # messages are never registered by this middleware on their own.
    if isinstance(chat_id,int) and chat_id>0:return chat_id
    if isinstance(chat_id,str) and chat_id.isdecimal():return int(chat_id)
    return None


class RegistryMiddleware:
    async def __call__(self,make_request,bot,method):
        # Closeable service messages are a universal UX rule. Preserve reply
        # keyboards (Telegram cannot attach an inline keyboard to one).
        if isinstance(method,(SendMessage,EditMessageText,SendDocument,SendPhoto,
                              SendVideo,SendVoice,EditMessageCaption,EditMessageReplyMarkup)):
            markup=getattr(method,'reply_markup',None)
            # An explicit edit_reply_markup(None) disables a stale card.
            # Never resurrect a keyboard intentionally removed by a handler.
            if isinstance(method,EditMessageReplyMarkup) and markup is None:
                pass
            elif markup is None or isinstance(markup,InlineKeyboardMarkup):
                old_rows=list(markup.inline_keyboard) if markup is not None else []
                has_close=any(button.callback_data=='ui:close'
                              for row in old_rows for button in row)
                # Active cancellation and informational Close must not coexist.
                has_cancel=any((button.callback_data or '').startswith('menugen:cancel')
                               for row in old_rows for button in row)
                if not has_close and not has_cancel:
                    close=styled('❌ Закрыть','ui:close','danger')
                    method=method.model_copy(update={'reply_markup':InlineKeyboardMarkup(
                        inline_keyboard=[*old_rows,[close]])})
        started=time.perf_counter()
        response=await make_request(bot,method)
        request_elapsed=time.perf_counter()-started
        from perf_diagnostics import add as perf_add
        perf_add("telegram_api:"+type(method).__name__,request_elapsed)
        if request_elapsed >= 0.7:
            log.warning("PERF03 telegram_api %s took %.3fs", type(method).__name__, request_elapsed)
        try:
            if isinstance(method,DeleteMessage):
                chat=_private_numeric_chat(method.chat_id)
                if chat:_schedule_registry("forget",chat,method.message_id)
            elif isinstance(method,(SendMessage,EditMessageText,SendDocument,SendPhoto,
                                    SendVideo,SendVoice,EditMessageCaption,EditMessageReplyMarkup)):
                markup=getattr(method,'reply_markup',None)
                if not markup or not getattr(markup,'inline_keyboard',None):return response
                chat=_private_numeric_chat(method.chat_id)
                if isinstance(response,Message):
                    chat=_private_numeric_chat(response.chat.id)
                    mid=response.message_id
                else:
                    mid=getattr(method,'message_id',None)
                if chat and mid:_schedule_registry("record",chat,mid)
        except Exception:
            log.warning('Cannot update card registry',exc_info=True)
        return response


async def close_cards(bot,chat_id):
    """Delete tracked bot cards; if Telegram refuses, disable buttons."""
    ids=tracked(chat_id)
    deleted=disabled=failed=0
    for mid in ids:
        try:
            await bot.delete_message(chat_id,mid)
            deleted+=1
        except Exception:
            try:
                await bot.edit_message_reply_markup(chat_id,mid,reply_markup=None)
                disabled+=1
            except Exception:
                failed+=1
        forget(chat_id,mid)
    return {'deleted':deleted,'disabled':disabled,'failed':failed}
