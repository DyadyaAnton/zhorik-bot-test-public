"""Единая проверка доступа к закрытой песочнице."""
import logging
import time
from typing import Optional
from aiogram.types import Message, CallbackQuery
from config import settings
from database import remember_user, is_allowed

log = logging.getLogger("zhorik.access")


def allowed(user_id: Optional[int]) -> bool:
    return user_id is not None and settings.sandbox_owner_id is not None and is_allowed(user_id, settings.sandbox_owner_id)


async def check_message(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if not allowed(user_id):
        log.info("Доступ в песочницу отклонён для user_id=%s", user_id)
        await message.answer("Жорик пока работает в закрытой песочнице. Доступ не разрешён.")
        return False
    remember_user(user_id)
    return True


async def check_callback(callback: CallbackQuery) -> bool:
    t0=time.perf_counter()
    permitted=allowed(callback.from_user.id)
    dt=time.perf_counter()-t0
    if dt >= 0.3:
        log.warning("PERF03 callback access check took %.3fs",dt)
    if not permitted:
        await callback.answer("Доступ не разрешён.", show_alert=True)
        return False
    return True
