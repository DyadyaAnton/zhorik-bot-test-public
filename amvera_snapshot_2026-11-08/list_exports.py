"""Only checked and positive-quantity products are included in shared exports."""
import io
from database import connect_db

def selected_rows(uid,lid):
    with connect_db() as db:
        title=db.execute('SELECT title FROM lists WHERE owner_id=? AND id=?',(uid,lid)).fetchone()
        if not title:return None
        rows=db.execute('SELECT text,quantity,unit FROM list_items WHERE list_id=? AND selected=1 AND quantity>0 ORDER BY position,id',(lid,)).fetchall()
        return title['title'],[dict(r) for r in rows]

def generate(uid,lid,fmt):
    payload=selected_rows(uid,lid)
    if payload is None:return None
    title,rows=payload
    if fmt=='txt':
        body='📋 '+title+'\n\n'+'\n'.join(f"{i}. {r['text']} — {r['quantity']:g} {r['unit']}" for i,r in enumerate(rows,1))
        return body.encode('utf-8')
    if fmt=='xlsx':
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb=Workbook();ws=wb.active;ws.title='Покупки'
        ws.append(['Товар','Количество','Единица'])
        for r in rows:ws.append([r['text'],r['quantity'],r['unit']])
        for cell in ws[1]:cell.font=Font(bold=True)
        for col,width in [('A',48),('B',18),('C',18)]:ws.column_dimensions[col].width=width
        out=io.BytesIO();wb.save(out);return out.getvalue()
    if fmt=='docx':
        from docx import Document
        doc=Document();doc.add_heading('Покупки — '+title,0)
        table=doc.add_table(rows=1,cols=3);table.style='Table Grid'
        for c,t in zip(table.rows[0].cells,['Товар','Количество','Единица']):c.text=t
        for r in rows:
            for c,t in zip(table.add_row().cells,[r['text'],f"{r['quantity']:g}",r['unit']]):c.text=t
        out=io.BytesIO();doc.save(out);return out.getvalue()
    raise ValueError('Unknown format')
