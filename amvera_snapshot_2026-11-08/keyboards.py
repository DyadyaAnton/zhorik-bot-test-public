from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from ui_config import STYLES, CLOSE, BACK, MAIN, PREV, NEXT, NEW_LIST


MAIN_MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📋 Мои списки"), KeyboardButton(text="🍽 Меню на неделю")],
    [KeyboardButton(text="💰 Бюджет и цены"), KeyboardButton(text="⚙️ Мои настройки")],
    [KeyboardButton(text="💬 Разработчику"), KeyboardButton(text="❓ Помощь")],
], resize_keyboard=True, input_field_placeholder="Выберите действие")

def main_menu_for(user_id):
    """Reply keyboard with an owner-only test-mode admin entry."""
    from config import settings
    owner_id = settings.sandbox_owner_id
    if owner_id is None or user_id != owner_id:
        return MAIN_MENU
    rows = [[KeyboardButton(text=button.text) for button in row] for row in MAIN_MENU.keyboard]
    rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               input_field_placeholder="Выберите действие")


def list_actions_keyboard(list_id):
    """Two compact action rows; rare actions are in a separate submenu."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('➕ Добавить',f'list:add:{list_id}','success'),
         styled('✏️ Изменить',f'list:editall:{list_id}'),
         styled('📄 Копия',f'list:copy:{list_id}')],
        [styled('📤 Экспорт',f'list:exports:{list_id}'), styled('⚙️ Ещё',f'list:more:{list_id}')],
        [styled(BACK,'list:back','primary'),styled(CLOSE,'ui:close','danger')],
    ])

def list_more_keyboard(list_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('✏️ Переименовать',f'list:rename:{list_id}')],
        [styled('✏️ Редактировать',f'list:editall:{list_id}')],
        [styled('🗑 Удалить список',f'list:delete:{list_id}','danger')],
        [styled(BACK,f'list:open:{list_id}')],
        [styled(CLOSE,'ui:close','danger')],
    ])

def list_export_keyboard(list_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('📄 TXT',f'list:export:{list_id}:txt'),
         styled('📊 Excel',f'list:export:{list_id}:xlsx'),
         styled('🖨 DOCX',f'list:export:{list_id}:docx')],
        [styled(BACK,f'list:open:{list_id}')],
        [styled(CLOSE,'ui:close','danger')],
    ])

def list_picker(rows, prefix="list:open:", page=0):
    count=len(rows);page=max(0,min(page,max(0,(count-1)//5)))
    buttons=[[InlineKeyboardButton(text=f"{page*5+i}. {row['title'][:40]}",callback_data=f"{prefix}{row['id']}")] for i,row in enumerate(rows[page*5:page*5+5],1)]
    nav=[]
    if page:nav.append(styled(PREV,f'list:overview:{page-1}',None))
    # No nonfunctional page counter
    if (page+1)*5<count:nav.append(styled(NEXT,f'list:overview:{page+1}'))
    if nav:buttons.append(nav)
    buttons.extend([[styled(NEW_LIST,"home:new","success")],[styled(CLOSE,"ui:close","danger")]])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_keyboard(token):
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled("✅ Сохранить", f"draft:save:{token}", "success"),
         InlineKeyboardButton(text="✏️ Исправить", callback_data=f"draft:edit:{token}")],
        [styled("❌ Отмена", f"draft:cancel:{token}", "danger")],
        [styled("❌ Закрыть", f"draft:close:{token}", "danger")],
    ])


def styled(text,data,style=None):
    kw={'text':text,'callback_data':data}
    # Bot API button styles require support in the installed aiogram schema.
    # When unsupported, retain the semantic emoji without crashing.
    if style and 'style' in getattr(InlineKeyboardButton,'model_fields',{}):
        kw['style']=style
    return InlineKeyboardButton(**kw)

def title_preview(token):
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('✅ Сохранить',f'title:save:{token}','success'),styled('✏️ Исправить',f'title:edit:{token}')],
        [styled('❌ Отмена',f'title:cancel:{token}','danger'),styled(CLOSE,f'title:close:{token}','danger')]])

def paged_list_keyboard(list_id, data):
    page=data['page'];count=data['count'];items=data['items'];rows=[]
    # A single checkbox above the list toggles every page, not just the visible page.
    all_checked=bool(data.get('all_selected',False))
    rows.append([styled('☑️ Отметить всё' if all_checked else '☐ Отметить всё',
                        f'list:clearall:{list_id}:{page}' if all_checked else f'list:selectall:{list_id}:{page}')])
    for item in items:
        # Names and quantities are displayed in the message above. The lower
        # controls have no repeated numbers, checkmarks, or quantity button.
        # Telegram clients determine actual inline button widths.
        name = str(item.get('text') or '').strip()
        toggle = f'item:toggle:{list_id}:{item["id"]}:{page}'
        rows.append([
            styled(name, toggle),
            styled('−', f'item:minus:{list_id}:{item["id"]}:{page}'),
            styled('+', f'item:plus:{list_id}:{item["id"]}:{page}'),
        ])
    nav=[]
    if page:nav.append(styled(PREV,f'list:page:{list_id}:{page-1}'))
    if (page+1)*5<count:nav.append(styled(NEXT,f'list:page:{list_id}:{page+1}'))
    if nav:rows.append(nav)
    rows.extend(list_actions_keyboard(list_id).inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def item_keyboard(list_id,item_id,page):
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('✏️ Изменить',f'item:edit:{list_id}:{item_id}:{page}')],
        [styled('🗑 Удалить',f'item:delete:{list_id}:{item_id}:{page}','danger')],
        [styled(BACK,f'list:page:{list_id}:{page}','primary')],
        [styled(CLOSE,'ui:close','danger')]])


# All price keyboards are defined centrally.
def price_home_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('📋 Общий каталог','price:show'),styled('📄 Общий отчёт TXT','price:catalog_txt')],
        [styled('🔎 Найти товар','price:search')],
        [styled('📊 Общий Excel','price:xlsx'),styled('🖨 Общий DOCX','price:docx')],
        [styled('📥 Импорт личных цен','price:upload'),styled('➕ Добавить цену','price:add')],
        [styled(CLOSE,'ui:close','danger')]])

def price_page_keyboard(page,total):
    nav=[]
    if page>0:nav.append(styled(PREV,f'price:page:{page-1}'))
    if (page+1)*10<total:nav.append(styled(NEXT,f'price:page:{page+1}'))
    return InlineKeyboardMarkup(inline_keyboard=([nav] if nav else [])+[
        [styled('🔎 Поиск товара','price:search')],
        [styled('📄 TXT','price:catalog_txt'),styled('📊 Excel','price:xlsx'),styled('🖨 DOCX','price:docx')],
        [styled('⬅️⬅️ Каталог и цены','price:home','primary'),styled(CLOSE,'ui:close','danger')]])

def price_confirm_keyboard(token):
    return InlineKeyboardMarkup(inline_keyboard=[
        [styled('✅ Сохранить',f'price:save:{token}','success')],
        [styled('❌ Отмена',f'price:cancel','danger')]])
