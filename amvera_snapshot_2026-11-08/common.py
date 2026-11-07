from aiogram import Router, F
from telegram_retry import safe_call
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, InlineKeyboardMarkup
from keyboards import styled
from access import check_message, check_callback
from aiogram.types import CallbackQuery
from keyboards import MAIN_MENU, main_menu_for
from nutrition_ui import keyboard as nutrition_keyboard
import texts

router = Router(name="common")
WELCOME_PAIRS = {}  # (chat_id, inline_card_id) -> greeting_id


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    if not await check_message(message):
        return
    await state.clear()
    # ReplyKeyboardMarkup and InlineKeyboardMarkup cannot coexist on one Telegram message.
    # Restore the private role-aware reply keyboard for every allowed user.
    await safe_call(message.answer, '🏠 Главное меню доступно ниже.',
                    reply_markup=main_menu_for(message.from_user.id))
    await safe_call(message.answer, texts.WELCOME + "\n\n" + texts.DISCLAIMER,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [styled("❌ Закрыть", "ui:close", "danger")]]))


@router.message(Command("begin"))
async def begin(message: Message, state: FSMContext):
    await start(message,state)

@router.message(Command("clean"))
async def clean(message: Message, state: FSMContext):
    if not await check_message(message):return
    from card_registry import tracked
    count=len(tracked(message.chat.id))
    await safe_call(message.answer,
        f'🧹 Найдено {count} зарегистрированных карточек Жорика. '
        'Очистка затронет также открытые справочники и рецепты. '
        'Списки, настройки и цены в базе останутся без изменений. '
        'Удалить карточки?',reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [styled('✅ Да, очистить','clean:confirm','danger')],
            [styled('⬅️⬅️ Отмена','clean:cancel','success')]]))


@router.callback_query(F.data.startswith('clean:'))
async def clean_decision(call:CallbackQuery,state:FSMContext):
    if not await check_callback(call):return
    action=call.data.split(':',1)[1]
    await call.answer()
    if action=='cancel':
        try:await call.message.delete()
        except Exception:await safe_call(call.message.edit_reply_markup,reply_markup=None)
        return
    if action!='confirm':return
    await state.clear()
    from menu_ui import cancel_generation_and_notify
    status=await cancel_generation_and_notify(call.from_user.id)
    if status=='saving':
        return await safe_call(call.message.answer,
            '⏳ Меню сейчас сохраняется. Дождитесь результата, затем повторите /clean.')
    from card_registry import close_cards
    result=await close_cards(call.bot,call.message.chat.id)
    await safe_call(call.message.answer,
        f'🧹 Удалено карточек: {result["deleted"]}; отключены кнопки: '
        f'{result["disabled"]}; не удалось закрыть: {result["failed"]}. '
        'Сохранённые данные не затронуты. Карточки до обновления могут '
        'закрываться только вручную.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[styled("✖️ Закрыть", "ui:close", "danger")]]))

@router.message(Command("help"))
async def help_command(message: Message, state: FSMContext):
    if not await check_message(message): return
    await state.clear()
    await safe_call(message.answer, "Главное меню доступно ниже.", reply_markup=main_menu_for(message.from_user.id) if message.from_user else MAIN_MENU)
    await safe_call(message.answer, ('❓ Справка Жорика\n\n📋 Новый список: «Мои списки» → «➕ Новый список» (или /new), введите название или произнесите «список на дачу», затем подтвердите сохранение.\n\n➕ Добавление: откройте список, нажмите «Добавить», перечислите товары и количества. Проверьте распознанное перед сохранением.\n\n✏️ Редактор: бот пришлёт инструкцию и ОТДЕЛЬНЫМ сообщением нумерованный список. Скопируйте только список, измените строки, отправьте и подтвердите. Номера перенумеруются.\n\n⏪⏩ Листание: по 5 позиций; нажмите номер для изменения пункта.\n\n⚙️ Мои настройки: любимые и нелюбимые продукты, аллергены.\n\n💰 Бюджет и цены: цены ориентировочные; скачайте TXT, исправьте его в Заметках и загрузите обратно. Для печати доступны Excel и DOCX.\n\n/cancel — отменить ввод.\n\n' + texts.DISCLAIMER), reply_markup=nutrition_keyboard())

@router.message(Command("cancel"))
@router.message(F.text == "Отмена")
async def cancel(message: Message, state: FSMContext):
    if not await check_message(message):
        return
    await state.clear()
    from menu_ui import cancel_generation_and_notify
    status=await cancel_generation_and_notify(message.from_user.id)
    label=('Генерация отменена. '+texts.CANCELLED if status=='cancelled' else
           'Меню уже сохраняется; дождитесь результата. Ввод отменён.' if status=='saving' else texts.CANCELLED)
    await safe_call(message.answer, label, reply_markup=main_menu_for(message.from_user.id) if message.from_user else MAIN_MENU)
