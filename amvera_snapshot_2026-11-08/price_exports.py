"""Read-only price exports; no data changes."""
import io
from price_storage import get_rows,category_for


def make_unified_xlsx(uid):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from database import connect_db
    from price07_unified_catalog import catalog_snapshot
    with connect_db() as db:
        snap = catalog_snapshot(db, uid, include_market=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Единый каталог'
    ws.append(['Код', 'Продукт', 'Статус', 'Наблюдений', 'Личных предложений'])
    for item in snap['products']:
        ws.append([item['code'], item['name'], item['status'],
                   item['retail_offers'], len(item['personal_offers'])])
    for cell in ws[1]: cell.font = Font(bold=True)
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    offers = wb.create_sheet('Мои предложения')
    offers.append(['Код продукта', 'Товар', 'Магазин', 'Цена', 'Ед.', 'Дата', 'Статус'])
    for item in snap['products']:
        for quote in item['personal_offers']:
            offers.append([item['code'], item['name'], quote['shop'], quote['price'],
                           quote['unit'], quote['observed_at'], 'Личная ориентировочная'])
    market = wb.create_sheet('Магазинные наблюдения')
    market.append(['SKU', 'Сеть', 'Товар', 'Код каталога', 'Цена SKU',
                   'Цена за единицу', 'Ед.', 'Магазин', 'Регион', 'Статус', 'Дата', 'Источник'])
    for quote in snap['market_offers']:
        market.append([quote['sku'], quote['retailer'], quote['title'], quote['product_code'],
                       quote['price'], quote['unit_price'], quote['unit'], quote['store'],
                       quote['region'] or 'не подтверждён', quote['status'], quote['date'], quote['source']])
    if snap['market_truncated']:
        market.append(['⚠️ Последние 5000 наблюдений; полный архив — SQLite backup'])
    # Excel interprets user/SKU text beginning with '=' as formula by default.
    # All external text remains literal, never executable spreadsheet content.
    for worksheet in wb:
        for row in worksheet:
            for cell in row:
                if isinstance(cell.value, str):
                    if cell.value.startswith(('=', '+', '-', '@')):
                        cell.value = "'" + cell.value
                    cell.data_type = 's'
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def make_unified_docx(uid):
    from docx import Document
    from database import connect_db
    from price07_unified_catalog import catalog_snapshot
    with connect_db() as db:
        snap = catalog_snapshot(db, uid, include_market=True)
    doc = Document()
    doc.add_heading('Жорик — единый каталог товаров и цен', 0)
    doc.add_paragraph('Наблюдения и личные предложения являются ориентирами до проверки.')
    doc.add_paragraph(f'Позиций: {snap["products_total"]}; '
                      f'наблюдений магазинов: {snap["retail_observations_total"]}; '
                      f'личных предложений: {snap["personal_offers_total"]}.')
    table = doc.add_table(rows=1, cols=4)
    table.style = 'Table Grid'
    for cell, title in zip(table.rows[0].cells,
                           ['Код', 'Продукт', 'Статус', 'Наблюдений']):
        cell.text = title
    for item in snap['products']:
        for cell, value in zip(table.add_row().cells,
                               [item['code'], item['name'], item['status'], str(item['retail_offers'])]):
            cell.text = value
    doc.add_heading('Мои предложения', level=1)
    for item in snap['products']:
        for quote in item['personal_offers']:
            doc.add_paragraph(f'{item["name"]}: {quote["price"]:.2f} ₽/{quote["unit"]}, '
                              f'{quote["shop"]}, {quote["observed_at"]}')
    doc.add_heading('Магазинные наблюдения', level=1)
    market = doc.add_table(rows=1, cols=5)
    market.style = 'Table Grid'
    for cell, label in zip(market.rows[0].cells,
                           ['Сеть / SKU', 'Товар', 'Цена', 'Статус', 'Дата']):
        cell.text = label
    for quote in snap['market_offers']:
        items = [f'{quote["retailer"]}/{quote["sku"]}', quote['title'],
                 f'{quote["price"]} ₽; {quote["unit_price"]} ₽/{quote["unit"]}',
                 f'{quote["status"]}; регион: {quote["region"] or "не подтверждён"}; '
                 f'код: {quote["product_code"] or "не привязан"}', quote['date']]
        for cell, value in zip(market.add_row().cells, items):
            cell.text = str(value)
    if snap['market_truncated']:
        doc.add_paragraph('⚠️ Выведены последние 5000 наблюдений; полный архив — SQLite backup.')
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()

def make_xlsx(uid):
    from openpyxl import Workbook
    from openpyxl.styles import Font,PatternFill
    from openpyxl.utils import get_column_letter
    wb=Workbook();ws=wb.active;ws.title='Цены'
    ws.append(['Товар','Категория','Магазин','Единица','Цена, ₽','Дата наблюдения'])
    for r in get_rows(uid):
        ws.append([r['product'],category_for(uid,r['product']),r['shop'],r['unit'],r['price'],r['observed_at']])
    for c in ws[1]:c.font=Font(bold=True)
    for col,width in enumerate([34,25,25,15,15,22],1):ws.column_dimensions[get_column_letter(col)].width=width
    ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
    out=io.BytesIO();wb.save(out);return out.getvalue()

def make_docx(uid):
    from docx import Document
    doc=Document();doc.add_heading('Жорик — Бюджет и цены',0)
    doc.add_paragraph('Цены ориентировочные. Дата наблюдения указана в таблице.')
    table=doc.add_table(rows=1,cols=5);table.style='Table Grid'
    for cell,name in zip(table.rows[0].cells,['Товар','Магазин','Цена, ₽','Ед.','Дата']):cell.text=name
    for r in get_rows(uid):
        for cell,value in zip(table.add_row().cells,[r['product'],r['shop'],f"{r['price']:.2f}",r['unit'],r['observed_at']]):cell.text=value
    out=io.BytesIO();doc.save(out);return out.getvalue()
