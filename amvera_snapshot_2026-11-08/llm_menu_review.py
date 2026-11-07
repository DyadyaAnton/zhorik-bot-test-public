"""Owner-only read-only saved-menu summary for LLM pilot 01B."""
import json
from database import connect_db


def menu_outline(owner_id, menu_id=None):
    """Read a particular menu from the same menus table as menu history; owner scoped."""
    with connect_db() as db:
        if menu_id is None:
            row = db.execute('SELECT id, result_json FROM menus WHERE owner_id=? ORDER BY id DESC LIMIT 1',
                             (owner_id,)).fetchone()
        else:
            row = db.execute('SELECT id, result_json FROM menus WHERE owner_id=? AND id=?',
                             (owner_id, menu_id)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row['result_json'])
        if not isinstance(payload,dict):
            return None
        saved_menu = payload.get('menu')
        # Current saves nest the week inside payload['menu']['menu'].
        # Older records may have stored the week directly in payload['menu'].
        days = saved_menu.get('menu',saved_menu) if isinstance(saved_menu,dict) else None
        if not isinstance(days,dict):
            return None
        lines=[]
        for day,meals in list(days.items())[:7]:
            if not isinstance(meals, dict):
                continue
            for meal, dishes in list(meals.items())[:5]:
                if not isinstance(dishes,list):
                    continue
                titles=[]
                for dish in dishes[:5]:
                    name = dish.get('name') if isinstance(dish,dict) else None
                    if isinstance(name,str) and name.strip():
                        titles.append(name.strip()[:90])
                if titles:
                    lines.append(str(day)[:40]+' / '+str(meal)[:40]+': '+', '.join(titles))
        # Preserve whole lines rather than truncating a meal in the middle.
        outline = ''
        for line in lines:
            if len(outline) + len(line) + 1 > 4000:
                break
            outline += ('\n' if outline else '') + line
        return (int(row['id']), outline) if outline else None
    except (ValueError,TypeError,KeyError):
        return None


def latest_menu_outline(owner_id):
    """Backward-compatible admin laboratory entry."""
    return menu_outline(owner_id)
