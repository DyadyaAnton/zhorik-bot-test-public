"""Durable menu pointers, named favorites and revisioned menu cards (REV5).

Existing menus are immutable; neither favorites nor refreshes overwrite their JSON.
All schema changes are additive so rolling back Python files remains possible.
"""
from __future__ import annotations
import hashlib
import json
import secrets
from database import connect_db

# Таблицы настроек: любое РЕАЛЬНОЕ изменение увеличивает «редакцию настроек» пользователя (см. settings_rev).
# Колонка пользователя и колонки, чьё изменение считается изменением настроек (повторная запись того же значения редакцию не меняет).
_REVISION_TABLES=(('zm_menu_budget','user_id',('budget',)),('zm_menu_people','user_id',('people',)),
                  ('zm_menu_modes','user_id',('mode',)),('user_settings','telegram_id',('calorie_target',)),
                  ('preferences','telegram_id',('preference_type','value','active')))


def _revision_trigger_sql():
    """Ожидаемые определения триггеров: имя -> SQL (имена таблиц и колонок из фиксированного списка выше)."""
    wanted={}
    for table,col,watched in _REVISION_TABLES:
        for event in ('INSERT','UPDATE','DELETE'):
            name=f'zm_rev_{table}_{event.lower()}'
            ref='OLD' if event=='DELETE' else 'NEW'
            when=' WHEN '+' OR '.join(f'OLD.{c} IS NOT NEW.{c}' for c in watched) if event=='UPDATE' else ''
            wanted[name]=(table,f"""CREATE TRIGGER {name} AFTER {event} ON {table}{when}
                BEGIN
                    INSERT INTO menu_runtime_settings(key,value) VALUES('u'||{ref}.{col}||':settings_rev','1')
                    ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1;
                END""")
    return wanted


def _ensure_revision_triggers(db):
    """Триггеры увеличивают счётчик u<id>:settings_rev при изменении настроек меню, калорий, типа питания, предпочтений
    и аллергенов (INSERT, UPDATE при изменении значения, DELETE).

    Пересоздаются только отсутствующие или устаревшие триггеры, и ВСЕ изменения идут одной транзакцией (BEGIN IMMEDIATE):
    при ошибке остаётся прежний набор. Возвращает список изменённых триггеров. Если все определения актуальны, ничего не пишется."""
    norm=lambda text:' '.join((text or '').split())
    existing={r[0]:r[1] for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'zm_rev_*'")}
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    wanted={name:sql for name,(table,sql) in _revision_trigger_sql().items() if table in tables}
    todo=[name for name,sql in wanted.items() if norm(existing.get(name))!=norm(sql)]
    stale=[name for name in existing if name not in wanted]
    if not todo and not stale:
        return []
    db.execute('BEGIN IMMEDIATE')
    try:
        for name in stale:
            db.execute(f'DROP TRIGGER IF EXISTS {name}')
        for name in todo:
            db.execute(f'DROP TRIGGER IF EXISTS {name}')
            db.execute(wanted[name])
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise
    return todo


def init():
    with connect_db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS menu_runtime_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS zm_menu_latest (
            owner_id INTEGER NOT NULL, parameters_key TEXT NOT NULL,
            menu_id INTEGER NOT NULL REFERENCES menus(id),
            PRIMARY KEY(owner_id, parameters_key));
        CREATE TABLE IF NOT EXISTS zm_menu_favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL, menu_id INTEGER NOT NULL REFERENCES menus(id),
            title TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(owner_id, menu_id));
        CREATE TABLE IF NOT EXISTS zm_menu_card_revisions (
            owner_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0, message_id INTEGER,
            PRIMARY KEY(owner_id, chat_id));
        CREATE TABLE IF NOT EXISTS zm_menu_price_versions (parent_menu_id INTEGER NOT NULL REFERENCES menus(id),new_menu_id INTEGER NOT NULL UNIQUE REFERENCES menus(id),PRIMARY KEY(parent_menu_id,new_menu_id));
        CREATE INDEX IF NOT EXISTS zm_menu_fav_owner ON zm_menu_favorites(owner_id,created_at);
        CREATE TABLE IF NOT EXISTS menu_shopping_lists(menu_id INTEGER PRIMARY KEY,list_id INTEGER NOT NULL);
        ''')
        _ensure_revision_triggers(db)


def key_for(people, budget, calories, diet, restrictions):
    def canonical(value):
        if isinstance(value,dict):return {str(k):canonical(v) for k,v in sorted(value.items())}
        if isinstance(value,(tuple,list,set)):return sorted((canonical(v) for v in value),key=lambda x:json.dumps(x,sort_keys=True,ensure_ascii=False,default=str))
        return value
    payload={'people':int(people),'budget_kopecks':round(float(budget)*100),
             'calories':int(calories or 0),'diet':str(diet),
             'restrictions':canonical(restrictions)}
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def remember(uid, key, menu_id, db=None):
    """Use caller transaction when supplied (generation INSERT + pointer are atomic)."""
    query=('INSERT INTO zm_menu_latest(owner_id,parameters_key,menu_id) VALUES(?,?,?) '
           'ON CONFLICT(owner_id,parameters_key) DO UPDATE SET menu_id=excluded.menu_id')
    if db is not None:db.execute(query,(uid,key,menu_id))
    else:
        with connect_db() as c:c.execute(query,(uid,key,menu_id))


def option_get(uid, name):
    """Разовая пользовательская опция (например, meat_cap) из таблицы menu_runtime_settings (создаётся в init()); None, если нет."""
    import sqlite3
    try:
        with connect_db() as db:
            row=db.execute('SELECT value FROM menu_runtime_settings WHERE key=?',(f'u{int(uid)}:{name}',)).fetchone()
    except sqlite3.OperationalError:  # таблица создаётся в init() при старте
        return None
    return row['value'] if row else None


def option_parse_int(raw):
    """Число из сырого значения опции вида «7#a1b2c3d4» (число и уникальная отметка установки); None, если не число."""
    try:return int(str(raw).split('#',1)[0]) if raw is not None else None
    except (TypeError,ValueError):return None


def option_get_int(uid, name):
    return option_parse_int(option_get(uid,name))


def option_set(uid, name, value):
    """Каждая установка получает уникальную отметку: повторный выбор того же значения отличается от прежнего."""
    with connect_db() as db:
        db.execute('INSERT INTO menu_runtime_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (f'u{int(uid)}:{name}',f'{value}#{secrets.token_hex(4)}'))


def settings_rev(uid):
    """Редакция настроек пользователя: растёт при любом изменении (триггеры _ensure_revision_triggers); 0, если не менялись."""
    return option_get_int(uid,'settings_rev') or 0


def option_clear(uid, name):
    with connect_db() as db:
        db.execute('DELETE FROM menu_runtime_settings WHERE key=?',(f'u{int(uid)}:{name}',))


def option_clear_if(uid, name, expected):
    """Снять опцию, только если её СЫРОЕ значение (с отметкой установки) всё ещё равно expected: повторный выбор того же числа не снимается."""
    with connect_db() as db:
        return db.execute('DELETE FROM menu_runtime_settings WHERE key=? AND value=?',
                          (f'u{int(uid)}:{name}',str(expected))).rowcount


def latest(uid, key):
    with connect_db() as db:
        row=db.execute('''SELECT m.* FROM zm_menu_latest p JOIN menus m ON m.id=p.menu_id
            WHERE p.owner_id=? AND p.parameters_key=? AND m.owner_id=?''',(uid,key,uid)).fetchone()
    return row


def favorite_add(uid,menu_id,title):
    title=title.strip()[:90] or 'Любимое меню'
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT 1 FROM menus WHERE id=? AND owner_id=?',(menu_id,uid)).fetchone()
        if not row:raise ValueError('Меню не найдено')
        cur=db.execute('''INSERT INTO zm_menu_favorites(owner_id,menu_id,title)
             VALUES(?,?,?) ON CONFLICT(owner_id,menu_id) DO NOTHING''',(uid,menu_id,title))
        return cur.rowcount==1  # False: this menu was already a favorite


def favorite_for_menu(uid,menu_id):
    with connect_db() as db:
        return db.execute('SELECT id FROM zm_menu_favorites WHERE owner_id=? AND menu_id=?',(uid,menu_id)).fetchone()


def favorite_toggle(uid,menu_id,title):
    """True if saved; False if unmarked. Historical menu itself is never deleted."""
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT 1 FROM menus WHERE id=? AND owner_id=?',(menu_id,uid)).fetchone()
        if not row:raise ValueError('Меню не найдено')
        existing=db.execute('SELECT id FROM zm_menu_favorites WHERE owner_id=? AND menu_id=?',(uid,menu_id)).fetchone()
        if existing:
            db.execute('DELETE FROM zm_menu_favorites WHERE id=? AND owner_id=?',(existing['id'],uid))
            return False
        db.execute('INSERT INTO zm_menu_favorites(owner_id,menu_id,title) VALUES(?,?,?)',(uid,menu_id,title[:90]))
        return True

def favorite_list(uid):
    with connect_db() as db:
        return db.execute('''SELECT f.id,f.menu_id,f.title,m.people_count,m.budget,
            m.calorie_target,m.created_at FROM zm_menu_favorites f
            JOIN menus m ON m.id=f.menu_id WHERE f.owner_id=? AND m.owner_id=?
            ORDER BY f.id DESC''',(uid,uid)).fetchall()


def favorite_rename(uid,favorite_id,title):
    title=str(title).strip()[:90]
    if not title:raise ValueError('Введите название меню')
    with connect_db() as db:
        updated=db.execute('UPDATE zm_menu_favorites SET title=? WHERE id=? AND owner_id=?',(title,favorite_id,uid))
        if updated.rowcount!=1:raise ValueError('Любимое меню не найдено')


def favorite_remove(uid,favorite_id):
    with connect_db() as db:
        db.execute('DELETE FROM zm_menu_favorites WHERE id=? AND owner_id=?',(favorite_id,uid))


def favorite_get(uid,favorite_id):
    with connect_db() as db:
        return db.execute('''SELECT f.id AS favorite_id,f.title AS favorite_title,m.*
            FROM zm_menu_favorites f JOIN menus m ON m.id=f.menu_id
            WHERE f.id=? AND f.owner_id=? AND m.owner_id=?''',(favorite_id,uid,uid)).fetchone()


def next_card(uid,chat_id):
    """Increment persisted monotonic revision BEFORE creating a new card."""
    with connect_db() as db:
        db.execute('''INSERT INTO zm_menu_card_revisions(owner_id,chat_id,revision,message_id)
            VALUES(?,?,1,NULL) ON CONFLICT(owner_id,chat_id)
            DO UPDATE SET revision=revision+1,message_id=NULL''',(uid,chat_id))
        return int(db.execute('SELECT revision FROM zm_menu_card_revisions WHERE owner_id=? AND chat_id=?',
                              (uid,chat_id)).fetchone()[0])


def card_bind(uid,chat_id,revision,message_id):
    with connect_db() as db:
        db.execute('''UPDATE zm_menu_card_revisions SET message_id=?
            WHERE owner_id=? AND chat_id=? AND revision=?''',(message_id,uid,chat_id,revision))


def card_valid(uid,chat_id,revision,message_id):
    with connect_db() as db:
        row=db.execute('''SELECT revision,message_id FROM zm_menu_card_revisions
            WHERE owner_id=? AND chat_id=?''',(uid,chat_id)).fetchone()
    return bool(row and row['revision']==int(revision) and row['message_id']==message_id)


def card_info(uid,chat_id):
    with connect_db() as db:
        return db.execute('''SELECT revision,message_id FROM zm_menu_card_revisions
            WHERE owner_id=? AND chat_id=?''',(uid,chat_id)).fetchone()


def history(uid,limit=15):
    with connect_db() as db:
        return db.execute('SELECT id,people_count,budget,calorie_target,created_at FROM menus WHERE owner_id=? ORDER BY id DESC LIMIT ?',(uid,limit)).fetchall()
