"""Изолированный слой SQLite. SQL-запросы проекта находятся здесь."""
import sqlite3
import time
from typing import Optional
from config import settings


class ClosingConnection(sqlite3.Connection):
    def execute(self, sql, parameters=(), /):
        t=time.perf_counter()
        try:
            return super().execute(sql, parameters)
        finally:
            from perf_diagnostics import add as perf_add
            elapsed=time.perf_counter()-t
            perf_add("sqlite_execute", elapsed)
            # Only a coarse SQL operation class is recorded, never a query,
            # parameter, user ID or product name. Detect intermittent locks.
            if elapsed>=0.5:
                verb=str(sql).lstrip().split(None,1)[0].upper() if str(sql).strip() else 'OTHER'
                category='read' if verb in ('SELECT','PRAGMA','WITH') else 'write'
                perf_add('sqlite_slow_'+category,elapsed)

    def executemany(self, sql, parameters, /):
        t=time.perf_counter()
        try:
            return super().executemany(sql, parameters)
        finally:
            from perf_diagnostics import add as perf_add
            perf_add("sqlite_executemany", time.perf_counter()-t)

    """Commit/rollback on context exit, then release SQLite handle."""
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_db() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    t=time.perf_counter()
    conn = sqlite3.connect(settings.db_path, timeout=10, isolation_level=None, factory=ClosingConnection)
    from perf_diagnostics import add as perf_add
    perf_add("sqlite_connect",time.perf_counter()-t)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with connect_db() as conn:
        # WAL is a persistent database setting; do not negotiate it per query.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS allowed_users (
            telegram_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
            calorie_target INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS preferences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            preference_type TEXT NOT NULL CHECK(preference_type IN ('like','dislike','allergy')),
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(telegram_id, preference_type, value)
        );
        CREATE TABLE IF NOT EXISTS lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS list_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            is_builtin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS template_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL DEFAULT 'шт',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            retailer TEXT NOT NULL,
            price REAL NOT NULL CHECK(price >= 0),
            currency TEXT NOT NULL DEFAULT 'RUB',
            observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL DEFAULT 'manual',
            UNIQUE(product_id, retailer, observed_at)
        );
        CREATE TABLE IF NOT EXISTS menus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
            people_count INTEGER NOT NULL DEFAULT 1,
            budget REAL,
            calorie_target INTEGER,
            result_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS menu_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            request_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'created',
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_lists_owner ON lists(owner_id);
        CREATE INDEX IF NOT EXISTS idx_items_list ON list_items(list_id, position);
        CREATE INDEX IF NOT EXISTS idx_preferences_user ON preferences(telegram_id);
        CREATE INDEX IF NOT EXISTS idx_prices_product ON prices(product_id, observed_at);
        CREATE INDEX IF NOT EXISTS idx_menus_owner ON menus(owner_id, created_at);
        """)
        # Build 13: additive migrations, preserving every existing item and preference.
        item_columns={r['name'] for r in conn.execute('PRAGMA table_info(list_items)')}
        for name,definition in [('quantity','REAL NOT NULL DEFAULT 1'),('unit',"TEXT NOT NULL DEFAULT 'шт.'"),('selected','INTEGER NOT NULL DEFAULT 1')]:
            if name not in item_columns: conn.execute(f'ALTER TABLE list_items ADD COLUMN {name} {definition}')
        pref_columns={r['name'] for r in conn.execute('PRAGMA table_info(preferences)')}
        if 'active' not in pref_columns: conn.execute('ALTER TABLE preferences ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
        # Базовые шаблоны добавляются безопасно и только если ещё не существуют.
        for title, tags, items in (
            ("На неделю", "еда", ("Овощи", "Фрукты", "Крупы", "Молочные продукты")),
            ("Шашлык", "еда", ("Мясо", "Лук", "Овощи", "Уголь", "Салфетки")),
            ("Уборка", "бытовое", ("Средство для поверхностей", "Губки", "Мешки для мусора")),
        ):
            row = conn.execute(
                "SELECT id FROM templates WHERE owner_id IS NULL AND title=? AND is_builtin=1", (title,)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO templates(owner_id,title,tags,is_builtin) VALUES(NULL,?,?,1)", (title,tags)
                )
                template_id = cur.lastrowid
                conn.executemany(
                    "INSERT INTO template_items(template_id,text,position) VALUES(?,?,?)",
                    [(template_id, item, i) for i, item in enumerate(items, 1)],
                )


def remember_user(user_id: int) -> None:
    with connect_db() as conn:
        # The common path is read-only. Avoid BEGIN IMMEDIATE on every /help,
        # /start, or text message after the user has already been registered.
        known = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM users WHERE telegram_id=?) "
            "AND EXISTS(SELECT 1 FROM user_settings WHERE telegram_id=?)",
            (user_id, user_id),
        ).fetchone()[0]
        if known:
            return
        # Concurrent first-time requests remain safe with INSERT OR IGNORE.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,))
        conn.execute("INSERT OR IGNORE INTO user_settings(telegram_id) VALUES (?)", (user_id,))


def is_allowed(user_id: int, owner_id: int) -> bool:
    if user_id == owner_id:
        return True
    with connect_db() as conn:
        return conn.execute("SELECT 1 FROM allowed_users WHERE telegram_id=?", (user_id,)).fetchone() is not None


def allow_user(user_id: int, added_by: int) -> None:
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO allowed_users(telegram_id,added_by) VALUES(?,?)", (user_id, added_by))
        conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (user_id,))
        conn.execute("INSERT OR IGNORE INTO user_settings(telegram_id) VALUES (?)", (user_id,))


def deny_user(user_id: int) -> bool:
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM allowed_users WHERE telegram_id=?", (user_id,))
        return cur.rowcount > 0


def list_allowed_users():
    with connect_db() as conn:
        return conn.execute("SELECT telegram_id, added_by, created_at FROM allowed_users ORDER BY created_at DESC").fetchall()


def create_list(owner_id: int, title: str, tags: str = "") -> int:
    title = title.strip()
    if not title:
        raise ValueError("Название списка не может быть пустым.")
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO users(telegram_id) VALUES (?)", (owner_id,))
        cur = conn.execute(
            "INSERT INTO lists(owner_id, title, tags) VALUES (?, ?, ?)",
            (owner_id, title[:100], tags.strip()[:250]),
        )
        return int(cur.lastrowid)


def get_lists(owner_id: int):
    with connect_db() as conn:
        return conn.execute(
            "SELECT id, title, tags, updated_at FROM lists WHERE owner_id=? ORDER BY updated_at DESC, id DESC",
            (owner_id,),
        ).fetchall()


def get_list(owner_id: int, list_id: int):
    with connect_db() as conn:
        return conn.execute(
            "SELECT id, title, tags FROM lists WHERE owner_id=? AND id=?", (owner_id, list_id)
        ).fetchone()


def add_items(owner_id: int, list_id: int, raw_text: str) -> int:
    import re
    from item_quantity import parse_quantity
    text = (raw_text or "").strip()
    if not text:
        return 0
    items = [line.strip().lstrip("•- \t") for line in text.splitlines()]
    items = [item for item in items if item]
    if len(items) == 1 and ("," in items[0] or ";" in items[0]):
        items = [part.strip() for part in re.split(r"[,;]", items[0]) if part.strip()]
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute("SELECT id FROM lists WHERE owner_id=? AND id=?", (owner_id, list_id)).fetchone()
        if not exists:
            return 0
        max_position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS p FROM list_items WHERE list_id=?", (list_id,)
        ).fetchone()["p"]
        conn.executemany(
            "INSERT INTO list_items(list_id,text,position,quantity,unit,selected) VALUES(?,?,?,?,?,?)",
            [(list_id, parse_quantity(item[:500])[0], max_position+i, parse_quantity(item[:500])[1], parse_quantity(item[:500])[2], int(parse_quantity(item[:500])[1]>0)) for i,item in enumerate(items,1)],
        )
        conn.execute("UPDATE lists SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (list_id,))
    return len(items)


def render_list(owner_id: int, list_id: int) -> Optional[str]:
    with connect_db() as conn:
        row = conn.execute("SELECT id,title,tags FROM lists WHERE owner_id=? AND id=?", (owner_id, list_id)).fetchone()
        if not row:
            return None
        items = conn.execute("SELECT text FROM list_items WHERE list_id=? ORDER BY position,id", (list_id,)).fetchall()
    result = [f"📋 {row['title']}"]
    if row["tags"]:
        result.append(f"Теги: {row['tags']}")
    result.append("")
    result.extend([f"• {item['text']}" for item in items] if items else ["Пока нет пунктов."])
    return "\n".join(result)


def delete_list(owner_id: int, list_id: int) -> bool:
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("DELETE FROM lists WHERE owner_id=? AND id=?", (owner_id, list_id))
        return cur.rowcount > 0


def rename_list(owner_id: int, list_id: int, title: str) -> bool:
    with connect_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("UPDATE lists SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND owner_id=?",
                           (title[:100], list_id, owner_id))
        return cur.rowcount > 0


def list_item_count(list_id: int) -> int:
    with connect_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM list_items WHERE list_id=?", (list_id,)).fetchone()[0]
