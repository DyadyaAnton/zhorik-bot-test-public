"""Owner-only price management; two-step destructive confirmations."""
import secrets
from aiogram import Router,F
from aiogram.filters import Command
from aiogram.types import CallbackQuery,Message,InlineKeyboardMarkup,FSInputFile
from config import settings
from keyboards import styled
from price_backup import create_backup,list_backups,count_prices,clear_prices,restore_backup
from telegram_retry import safe_call
router=Router(name='admin_prices')
TOKENS={}
def owner(uid):return uid==settings.sandbox_owner_id

def keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('🔄 Актуализировать цены','ap:refresh_prices','primary')],
        [styled('📊 Состояние источников','ap:refresh_status')],
        [styled('📊 Статистика','ap:stats')],
        [styled('📦 Создать копию','ap:backup'),styled('📂 Архивы (5)','ap:files')],
        [styled('🧹 Очистить тестовые цены','ap:clear_test','danger')],
        [styled('🗑 Очистить все цены','ap:clear_all','danger')],
        [styled('⬅️⬅️ Главное меню','ap:home','primary'),styled('❌ Закрыть','ui:close','danger')]])

@router.message(Command('admin_prices'))
async def panel(m:Message):
    if not m.from_user or not owner(m.from_user.id):return
    await safe_call(m.answer,'🔐 Управление ценами\nПеред очисткой создаётся резервная копия.',reply_markup=keyboard())

@router.callback_query(F.data.startswith('ap:'))
async def callbacks(c:CallbackQuery):
    if not owner(c.from_user.id):return await c.answer('Только владелец',show_alert=True)
    bits=c.data.split(':');act=bits[1]
    try:
        if act=='stats':
            counts=count_prices();msg='📊 База цен\n'+'\n'.join(f'{k}: {v}' for k,v in counts.items())
            await safe_call(c.message.edit_text,msg,reply_markup=keyboard())
        elif act == 'refresh_prices':
            from price07_rev2_runner import cycle
            await c.answer('Запущена порция обновления')
            outcome = await cycle(per_source=4)
            await safe_call(c.message.answer,
                            '⏳ Обновление уже идёт.' if outcome['status']=='busy'
                            else '✅ Обработана очередная порция. Состояние — в админке.')
            return
        elif act == 'refresh_status':
            import asyncio
            from price07_rev2_runner import report
            from price07_ui import _rev2_format
            await safe_call(c.message.answer, _rev2_format(await asyncio.to_thread(report)))
        elif act=='backup':
            path=create_backup();await safe_call(c.message.answer,'Резервная копия создана: '+path.name)
        elif act=='files':
            files=list_backups();rows=[]
            for i,f in enumerate(files):
                rows.append([styled('⬇️ '+f.name[14:33],f'ap:download:{i}'),styled('♻️ Восстановить',f'ap:restore:{i}','danger')])
            rows.append([styled('⬅️⬅️ Назад','ap:back','primary'),styled('❌ Закрыть','ui:close','danger')])
            await safe_call(c.message.edit_text,'📂 Последние копии (новые сверху)' if files else 'Копий пока нет.',reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        elif act=='download':
            files=list_backups();i=int(bits[2]);
            if i>=len(files):return await c.answer('Копия устарела',show_alert=True)
            await safe_call(c.message.answer_document,FSInputFile(files[i]),caption='Архив цен (без списков покупок).')
        elif act in ('clear_all','clear_test','restore'):
            index=int(bits[2]) if act=='restore' else None
            if act=='restore' and index>=len(list_backups()):return await c.answer('Копия устарела',show_alert=True)
            token=secrets.token_hex(8);TOKENS[c.from_user.id]=(token,act,index)
            detail=('ВСЕ ценовые записи' if act=='clear_all' else 'тестовые ценовые записи' if act=='clear_test' else 'цены из выбранной копии')
            await safe_call(c.message.edit_text,f'⚠️ Подтвердите операцию: {detail}.\nСписки покупок и настройки не затрагиваются. Перед действием создаётся резервная копия.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[styled('🗑 Подтвердить',f'ap:confirm:{token}','danger')],[styled('✅ Отмена','ap:back','success')]]))
        elif act=='confirm':
            saved=TOKENS.pop(c.from_user.id,None)
            if not saved or bits[2]!=saved[0]:return await c.answer('Кнопка устарела',show_alert=True)
            _,kind,index=saved
            if kind=='restore':
                restored,safeguard=restore_backup(index)
                msg=f'♻️ Восстановлены цены из {restored.name}.\nСтраховочная копия: {safeguard.name}'
            else:
                n,path=clear_prices(test_only=(kind=='clear_test'))
                msg=f'✅ Удалено записей: {n}.\nРезервная копия: {path.name}'
            await safe_call(c.message.edit_text,msg,reply_markup=keyboard())
        elif act=='home':
            from home_ui import home_keyboard
            TOKENS.pop(c.from_user.id,None)
            await safe_call(c.message.edit_text,'🐾 Главное меню Жорика',reply_markup=home_keyboard())
        elif act=='back':
            TOKENS.pop(c.from_user.id,None)
            await safe_call(c.message.edit_text,'🔐 Управление ценами',reply_markup=keyboard())
    except Exception:
        import logging;logging.exception('Ошибка админской операции с ценами')
        await safe_call(c.message.answer,'⚠️ Операция не завершена. Данные проверьте в статистике; при ошибке очистки сохранена резервная копия.')
    await c.answer()
