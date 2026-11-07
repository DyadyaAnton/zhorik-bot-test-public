"""Build 09: price-only display, real TXT, conservative deduplication."""
import io
import secrets
from datetime import date
from aiogram import Router,F,Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup,BufferedInputFile
from access import check_message,check_callback
from keyboards import styled,price_home_keyboard,price_page_keyboard,price_confirm_keyboard
from telegram_retry import safe_call
from price_storage import get_rows,export_txt,parse_upload,apply,apply_categories,category_for
from database import connect_db
from ui_config import DISCLAIMER

router=Router(name='price_ui')
class PriceStates(StatesGroup):
    file=State()
    add=State()
    search=State()

def kb():
    return price_home_keyboard()

def price_overview(uid):
    """Unified catalog summary; personal offers are not a second price list."""
    from price07_unified_catalog import catalog_snapshot
    with connect_db() as db:
        snapshot = catalog_snapshot(db, uid)
    return ('💰 Единый каталог товаров и цен\n'
            f'📦 Товарных позиций: {snapshot["products_total"]}\n'
            f'🏪 Магазинных наблюдений: {snapshot["retail_observations_total"]}; '
            f'проверенных связей: {snapshot["retail_verified_linked_total"]}\n'
            f'🔗 SKU без подтверждённой связи: {snapshot["retail_unlinked_sku_groups"]}\n'
            f'💛 Личных ценовых предложений: {snapshot["personal_offers_total"]}\n'
            f'🔎 Личных товаров без однозначной связи: {snapshot["personal_unlinked_products"]}\n'
            '⚠️ Кандидаты остаются неподтверждёнными. '
            'Личные предложения доступны только вам.')


def _price_overview_legacy(uid):
    """Historical diagnostic retained for compatibility/audit only."""
    personal = len(get_rows(uid))
    retail = {}
    eligible = {}
    with connect_db() as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {'pb_skus', 'pb_observations'} <= names:
            for shop, review, total in db.execute('''
                SELECT s.retailer, o.review_status, COUNT(*)
                FROM pb_observations o JOIN pb_skus s ON s.id=o.sku_id
                GROUP BY s.retailer,o.review_status'''):
                retail.setdefault(shop, {})[review] = total
            if 'pb_products' in names:
                for shop, total in db.execute('''
                    SELECT s.retailer, COUNT(*) FROM pb_observations o
                    JOIN pb_skus s ON s.id=o.sku_id
                    JOIN pb_products p ON p.code=s.product_code
                    WHERE o.review_status IN ('auto_verified','manually_verified')
                      AND s.match_status IN ('auto_verified','manually_verified')
                      AND o.region_confirmed=1 AND TRIM(o.region)!=''
                      AND o.base_unit=p.base_unit AND o.unit_price_rub>0
                    GROUP BY s.retailer'''):
                    eligible[shop] = total
        estimate_count = 0
        try:
            from price_b_catalog import seeds
            estimate_count = len(seeds())
        except (ImportError, TypeError):
            pass
    total = sum(sum(statuses.values()) for statuses in retail.values())
    verified = sum(eligible.values())
    lines = ['💰 Бюджет и цены',
             f'📝 Мои цены: {personal} записей (ввод вручную / импорт).',
             f'🏪 Предложения магазинов: {total} наблюдений, регионально проверено {verified}.',
             f'🧮 Расчётный справочник: {estimate_count} продуктов (ориентиры, не магазинные цены).']
    if retail:
        lines.append('\nИсточники наблюдений:')
        for shop, statuses in sorted(retail.items()):
            n = sum(statuses.values())
            confirmed = eligible.get(shop,0)
            lines.append(f'• {shop}: {n}, регионально проверено {confirmed}')
    lines.extend(['', '⚠️ Проверка региона не означает совпадения с вашим регионом',
                  'или актуальности предложения. Непроверенные цены не подменяют смету.'])
    return '\n'.join(lines)

def nav(page,total):
    return price_page_keyboard(page,total)

def price_page(uid,page=0):
    from price07_unified_catalog import catalog_snapshot
    with connect_db() as db:
        snap = catalog_snapshot(db, uid)
    products = snap['products']
    total = len(products)
    page = max(0, min(page, max(0, (total-1)//10)))
    start = page*10
    lines = [f'💰 Единый каталог · {start+1 if total else 0}–{min(start+10,total)} из {total}', '']
    for index, item in enumerate(products[start:start+10], start+1):
        marks = []
        if item['personal_offers']:
            marks.append(f'💛 {len(item["personal_offers"])} личн.')
        if item['retail_offers']:
            marks.append(f'🏪 {item["retail_offers"]} наблюд.')
        if item.get('potential_unlinked_observations'):
            marks.append(f'🔎 {item["potential_unlinked_observations"]} возможных, связь не проверена')
        if item['status'] in ('review_required', 'personal_unlinked'):
            marks.append('⚠️ требуется проверка связи')
        lines.append(f'{index}. {item["name"]}' + (' · ' + ', '.join(marks) if marks else ''))
    lines.append('\n⚠️ Наблюдения не означают подтверждённую рыночную цену. '
                 'Личные предложения не видны другим пользователям.')
    return '\n'.join(lines), nav(page,total)


def _price_page_legacy(uid,page=0):
    from collections import defaultdict
    rows=get_rows(uid)
    with connect_db() as db:
        selected={r[0].casefold() for r in db.execute('SELECT shop FROM user_shops WHERE user_id=?',(uid,))}
    grouped=defaultdict(dict)
    for r in rows:
        # One observation per store per product and unit.
        key=(r['product'].casefold(),r['unit'])
        store=r['shop'].casefold()
        prev=grouped[key].get(store)
        if prev is None or r['observed_at']>=prev['observed_at']:
            grouped[key][store]=r
    items=[]
    for (name,unit),stores in grouped.items():
        vals=list(stores.values())
        import re
        test=[r for r in vals if re.search(r'тестов|test|демо|demo',r['shop'],re.I)]
        regional=[r for r in vals if re.search(r'москв|регион|статист|средн',r['shop'],re.I) and r not in test]
        retail=[r for r in vals if r not in test and r not in regional]
        chosen=([r for r in retail if r['shop'].casefold() in selected] if selected else retail) or regional or test
        if not chosen:continue
        avg=sum(r['price'] for r in chosen)/len(chosen)
        items.append((chosen[0]['product'],unit,avg,len(chosen),max(r['observed_at'] for r in chosen), '🧪 тест' if chosen[0] in test else '📍 регион' if chosen[0] in regional else '🏪 магазин'))
    items.sort(key=lambda r:r[0].casefold())
    total=len(items)
    page=max(0,min(page,max(0,(total-1)//10)))
    if not total:
        return '💰 Бюджет и цены\n\nНет цен для выбранных магазинов. Добавьте цену или загрузите TXT.',nav(0,0)
    start=page*10
    latest=max(r[4] for r in items)
    lines=[f'💰 Бюджет и цены · {start+1}–{min(start+10,total)} из {total}',
           f'Последнее наблюдение: {latest} · Стр. {page+1}/{(total+9)//10}','']
    for idx,(name,unit,avg,n,dt,source) in enumerate(items[start:start+10],start+1):
        lines.append(f'{idx}. {name} — {avg:.2f} ₽/{unit}')
    lines.append('\n⚠️ Цены ориентировочные. Даты проверки — в TXT/Excel.')
    return '\n'.join(lines),nav(page,total)

@router.message(Command('myprices'))
@router.message(Command('catalog_prices'))
@router.message(F.text.in_({'💰 Мои цены','💰 Бюджет и цены'}))
async def myprices(m:Message):
    if not await check_message(m):return
    import asyncio
    body=await asyncio.to_thread(price_overview,m.from_user.id)
    await safe_call(m.answer,body,reply_markup=kb())

@router.callback_query(F.data.startswith('price:'))
async def action(c:CallbackQuery,state:FSMContext):
    if not await check_callback(c):return
    p=c.data.split(':');act=p[1];uid=c.from_user.id
    if act in ('show','page'):
        page=int(p[2]) if act=='page' and len(p)>2 and p[2].isdigit() else 0
        import asyncio
        msg,buttons=await asyncio.to_thread(price_page,uid,page)
        await safe_call(c.message.edit_text,msg,reply_markup=buttons)
    elif act=='home':
        import asyncio
        body=await asyncio.to_thread(price_overview,uid)
        await safe_call(c.message.edit_text,body,reply_markup=kb())
    elif act=='search':
        await state.update_data(catalog_search_codes=[],catalog_search_token=None)
        await state.set_state(PriceStates.search)
        await safe_call(c.message.answer,'🔎 Напишите название товара или его часть (до 100 символов).')
    elif act=='detail':
        token=p[3] if len(p)>3 else ''
        index=int(p[2]) if len(p)>2 and p[2].isdigit() else -1
        data=await state.get_data()
        codes=data.get('catalog_search_codes') or []
        if token!=data.get('catalog_search_token') or not 0<=index<len(codes):
            return await c.answer('Результаты поиска устарели. Повторите поиск.',show_alert=True)
        from price07_unified_catalog import catalog_product_detail
        import asyncio
        def load_detail():
            with connect_db() as db:
                return catalog_product_detail(db,uid,codes[index])
        try: detail=await asyncio.to_thread(load_detail)
        except (KeyError,ValueError):
            return await c.answer('Товар не найден. Повторите поиск.',show_alert=True)
        item=detail['product']
        lines=[f'📦 {item["name"]}',f'Статус: {item["status"]}',
               f'Магазинных наблюдений: {item["retail_offers"]}',
               f'Личных предложений: {len(item["personal_offers"])}','']
        if item.get('potential_unlinked_observations'):
            lines.append(f'🔎 Непривязанных совпадений по названию: {item["potential_unlinked_observations"]} '
                         '(товар и SKU НЕ подтверждены как совпадающие).')
        if item['personal_offers']:
            lines.append('💛 Ваши предложения:')
            for offer in item['personal_offers'][:10]:
                lines.append(f'• {offer["shop"]}: {offer["price"]:.2f} ₽/{offer["unit"]} · {offer["observed_at"]}')
        if detail['history']:
            lines.append('🏪 История магазинных наблюдений (до 20):')
            for quote in detail['history']:
                lines.append(f'• {quote["retailer"]}: {quote["price"]} ₽/SKU '
                             f'· {quote["observed_at"]} · {quote["region"] or "регион не подтверждён"} '
                             f'· {quote["quality"]}')
        else:lines.append('Магазинная история для этого товара пока отсутствует.')
        lines.append('⚠️ История — не гарантия актуальной цены или доступности товара.')
        buttons=InlineKeyboardMarkup(inline_keyboard=[
            [styled('🔎 Новый поиск','price:search')],
            [styled('⬅️⬅️ Каталог и цены','price:home'),styled('❌ Закрыть','ui:close','danger')]])
        await safe_call(c.message.edit_text,'\n'.join(lines)[:4000],reply_markup=buttons)
    elif act=='searchpage':
        token=p[3] if len(p)>3 else ''
        offset=int(p[2]) if len(p)>2 and p[2].isdigit() else -1
        data=await state.get_data()
        if token!=data.get('catalog_search_token') or not 0<=offset<=100000 \
                or not isinstance(data.get('catalog_search_query'),str):
            return await c.answer('Результаты поиска устарели. Повторите поиск.',show_alert=True)
        await _catalog_search_page(uid,state,data['catalog_search_query'],offset,c.message.edit_text)
    elif act=='main':
        await state.clear()
        await safe_call(c.message.delete)
    elif act in ('xlsx','docx'):
        import asyncio
        from price_exports import make_unified_xlsx,make_unified_docx
        data=await asyncio.to_thread(make_unified_xlsx if act=='xlsx' else make_unified_docx,uid)
        filename='jorik_unified_catalog.xlsx' if act=='xlsx' else 'jorik_unified_catalog.docx'
        await safe_call(c.message.answer_document,BufferedInputFile(data,filename=filename),caption='Единый каталог; личные предложения только ваши.')
    elif act=='txt':
        await safe_call(c.message.answer_document,BufferedInputFile(export_txt(uid).encode('utf-8-sig'),filename='my_prices.txt'),caption='Настоящий TXT: откройте в Заметках, исправьте строки и загрузите обратно. Удаление строки из файла не удаляет запись в боте.')
    elif act=='catalog_txt':
        from price07_unified_catalog import export_catalog_txt
        import asyncio
        def make_payload():
            with connect_db() as db:
                return export_catalog_txt(db, uid)
        payload = await asyncio.to_thread(make_payload)
        await safe_call(c.message.answer_document,BufferedInputFile(payload.encode('utf-8-sig'),filename='jorik_unified_catalog.txt'),caption='Единый каталог. Личные предложения видны только вам.')
    elif act=='upload':
        await state.set_state(PriceStates.file)
        await safe_call(c.message.answer,'Пришлите файл .txt (до 1 МБ). Формат:\n[Курица]\nКуриное бедро | 350 | кг | Магнит | 2026-10-26\n\nПоддерживается также прежний CSV и Excel. Перед сохранением будет проверка. Строки, которых нет в файле, не удаляются.')
    elif act=='add':
        await state.set_state(PriceStates.add)
        await safe_call(c.message.answer,'Пришлите одну строку:\nКуриное бедро | 350 | кг | Магнит | 2026-10-26\nДата — день наблюдения, цена в рублях за кг/л/шт.\nМожно указать категорию отдельной строкой [Курица].')
    elif act=='save':
        d=await state.get_data()
        if d.get('price_token')!=(p[2] if len(p)>2 else ''):return await c.answer('Кнопка устарела',show_alert=True)
        try:
            n=apply(uid,d['price_rows'])
            nc=apply_categories(uid,d.get('price_catalog',[]))
        except Exception as e:
            return await c.answer('Ошибка сохранения: '+str(e)[:100],show_alert=True)
        await state.clear()
        await safe_call(c.message.edit_text,f'✅ Сохранено цен: {n}. Товаров с категориями: {nc}. Остальные записи не удалены.',reply_markup=kb())
    elif act=='cancel':
        await state.clear()
        await safe_call(c.message.edit_text,'❌ Отмена. Цены не изменены.',reply_markup=kb())
    await c.answer()

async def _catalog_search_page(uid,state,query,offset,send):
    from price07_unified_catalog import search_catalog
    import asyncio
    def load():
        with connect_db() as db:
            return search_catalog(db,uid,query,limit=12,offset=offset)
    rows,total=await asyncio.to_thread(load)
    token=secrets.token_hex(5)
    await state.update_data(catalog_search_codes=[item['code'] for item in rows],
                            catalog_search_token=token,catalog_search_query=query)
    await state.set_state(None)
    body=[f'🔎 «{query}»: найдено {total}; показано {offset+1 if rows else 0}–{offset+len(rows)}.']
    if not rows:body.append('Совпадений нет. Попробуйте другое название.')
    buttons=[[styled(f'{idx+1}. {item["name"][:38]}',f'price:detail:{idx}:{token}')]
             for idx,item in enumerate(rows)]
    nav=[]
    if offset>0:nav.append(styled('⬅️⬅️',f'price:searchpage:{max(0,offset-12)}:{token}'))
    if offset+len(rows)<total:nav.append(styled('➡️➡️',f'price:searchpage:{offset+12}:{token}'))
    if nav:buttons.append(nav)
    buttons.append([styled('🔎 Новый поиск','price:search')])
    buttons.append([styled('⬅️⬅️ Каталог и цены','price:home'),styled('❌ Закрыть','ui:close','danger')])
    await safe_call(send,'\n'.join(body),reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.message(PriceStates.search,F.text)
async def search_price_catalog(m:Message,state:FSMContext):
    if not await check_message(m):return
    query=m.text.strip()
    if not 1<=len(query)<=100:
        return await safe_call(m.answer,'Введите от 1 до 100 символов.')
    await _catalog_search_page(m.from_user.id,state,query,0,m.answer)

async def preview(m,state,rows,errors,catalog):
    if not rows:return await safe_call(m.answer,f'Нет корректных цен. Ошибки в строках: {errors[:15]}')
    token=secrets.token_hex(6)
    await state.update_data(price_rows=rows,price_catalog=catalog,price_token=token)
    await state.set_state(None)
    buttons=price_confirm_keyboard(token)
    sample='\n'.join(f'• {r[0]} — {r[3]:.2f} ₽/{r[2]} · {r[1]}' for r in rows[:5])
    await safe_call(m.answer,f'Проверка перед импортом:\nЦен: {len(rows)}\nКатегорий/товаров: {len(catalog)}\nОшибочных строк: {len(errors)}'+(f' ({errors[:15]})' if errors else '')+'\n\n'+sample+'\n\nОстальные записи сохранятся. Подтвердить?',reply_markup=buttons)

@router.message(PriceStates.add,F.text)
async def add_line(m:Message,state:FSMContext):
    if not await check_message(m):return
    text=m.text.strip()
    if not text.startswith('['):text='[Иное]\n'+text
    rows,errors,catalog=parse_upload(text.encode('utf-8'))
    await preview(m,state,rows,errors,catalog)

@router.message(PriceStates.file,F.document)
async def uploaded(m:Message,state:FSMContext,bot:Bot):
    if not await check_message(m):return
    doc=m.document
    if doc.file_size and doc.file_size>1024*1024:return await safe_call(m.answer,'Файл больше 1 МБ.')
    name=(doc.file_name or '').lower()
    if not name.endswith(('.txt','.csv','.xlsx')):return await safe_call(m.answer,'Нужен TXT, CSV или XLSX.')
    try:
        f=await bot.get_file(doc.file_id);data=(await bot.download_file(f.file_path)).read()
        if len(data)>1024*1024:raise ValueError('Файл больше 1 МБ')
        if name.endswith('.xlsx'):
            from openpyxl import load_workbook
            wb=load_workbook(io.BytesIO(data),read_only=True,data_only=True)
            catalog=[]
            # Ignore the 200-item empty catalog: only priced rows are imported.
            ws=wb['Импорт в Жорик'] if 'Импорт в Жорик' in wb.sheetnames else wb.active
            rows=ws.iter_rows(values_only=True);head=next(rows,None)
            if not head or tuple(str(x) for x in head[:5])!=('product','shop','unit','price','observed_at'):raise ValueError('Неверные колонки Excel')
            import csv
            s=io.StringIO();w=csv.writer(s,delimiter=';');w.writerow(['product','shop','unit','price','observed_at','category'])
            for r in rows:
                if not r or not any(x is not None for x in r[:5]):continue
                w.writerow([str(x) if x is not None else '' for x in (list(r[:6])+[''])[:6]])
            data=s.getvalue().encode('utf-8')
            prices,errors,inline=parse_upload(data);catalog+=inline
        else:prices,errors,catalog=parse_upload(data)
    except Exception as e:return await safe_call(m.answer,'Ошибка файла: '+str(e)[:200])
    await preview(m,state,prices,errors,catalog)
