"""List item CRUD with per-user authorization."""
from database import connect_db

def get_page(owner_id, list_id, page=0, size=5):
    with connect_db() as db:
        row=db.execute("SELECT id,title FROM lists WHERE id=? AND owner_id=?",(list_id,owner_id)).fetchone()
        if not row:return None
        count=db.execute("SELECT COUNT(*) FROM list_items WHERE list_id=?",(list_id,)).fetchone()[0]
        page=max(0,min(page,max(0,(count-1)//size)))
        all_selected=bool(count and db.execute('SELECT COUNT(*) FROM list_items WHERE list_id=? AND (selected=0 OR quantity<=0)',(list_id,)).fetchone()[0]==0)
        items=db.execute("SELECT id,text,quantity,unit,selected FROM list_items WHERE list_id=? ORDER BY position,id LIMIT ? OFFSET ?",(list_id,size,page*size)).fetchall()
        return dict(title=row['title'],count=count,page=page,all_selected=all_selected,items=[dict(i) for i in items])

def get_item(owner_id,list_id,item_id):
    with connect_db() as db:
        return db.execute("SELECT i.id,i.text,i.quantity,i.unit,i.selected FROM list_items i JOIN lists l ON l.id=i.list_id WHERE l.owner_id=? AND l.id=? AND i.id=?",(owner_id,list_id,item_id)).fetchone()

def update_item(owner_id,list_id,item_id,text):
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        c=db.execute("UPDATE list_items SET text=? WHERE id=? AND list_id=? AND EXISTS(SELECT 1 FROM lists WHERE id=? AND owner_id=?)",(text[:500],item_id,list_id,list_id,owner_id))
        return c.rowcount>0

def delete_item(owner_id,list_id,item_id):
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        c=db.execute("DELETE FROM list_items WHERE id=? AND list_id=? AND EXISTS(SELECT 1 FROM lists WHERE id=? AND owner_id=?)",(item_id,list_id,list_id,owner_id))
        return c.rowcount>0


def change_selection(owner_id,list_id,item_id,action):
    """Atomic, owner-scoped toggle or quantity change. A manual toggle preserves quantity."""
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT i.quantity,i.selected,i.unit FROM list_items i JOIN lists l ON l.id=i.list_id WHERE l.owner_id=? AND l.id=? AND i.id=?',(owner_id,list_id,item_id)).fetchone()
        if row is None:return False
        qty=float(row['quantity']);selected=int(row['selected']);unit=row['unit']
        step=0.5 if unit in ('кг','л') else (100 if unit in ('г','мл') else 1)
        if action=='toggle':
            selected=1-selected
            if selected and qty==0:qty=step
        elif action=='plus':qty=round(qty+step,3);selected=1
        elif action=='minus':qty=round(max(0,qty-step),3);selected=0 if qty==0 else selected
        else:return False
        db.execute('UPDATE list_items SET quantity=?,selected=? WHERE id=?',(qty,selected,item_id))
        return True

def set_selection_all(owner_id,list_id,selected):
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT 1 FROM lists WHERE owner_id=? AND id=?',(owner_id,list_id)).fetchone()
        if not row:return False
        db.execute('UPDATE list_items SET selected=CASE WHEN quantity>0 THEN ? ELSE 0 END WHERE list_id=?',(int(bool(selected)),list_id))
        return True

def selected_export(owner_id,list_id):
    with connect_db() as db:
        row=db.execute('SELECT title FROM lists WHERE owner_id=? AND id=?',(owner_id,list_id)).fetchone()
        if not row:return None
        items=db.execute('SELECT text,quantity,unit FROM list_items WHERE list_id=? AND selected=1 AND quantity>0 ORDER BY position,id',(list_id,)).fetchall()
    if not items:return '📋 '+row['title']+'\nНет выбранных товаров.'
    return '📋 '+row['title']+'\n\n'+'\n'.join(f"{i}. {r['text']} — {r['quantity']:g} {r['unit']}" for i,r in enumerate(items,1))
