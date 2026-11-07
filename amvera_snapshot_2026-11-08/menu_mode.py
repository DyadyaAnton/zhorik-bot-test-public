"""Persistent global owner-only menu provider switch."""
from database import connect_db
MODES=('local','deepseek','openai','amvera')
def init():
    with connect_db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS menu_runtime_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
def get_mode():
    with connect_db() as db:
        row=db.execute("SELECT value FROM menu_runtime_settings WHERE key='provider'").fetchone()
    return row['value'] if row and row['value'] in MODES else 'local'
def set_mode(mode):
    if mode not in MODES:raise ValueError('Unknown mode')
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO menu_runtime_settings(key,value) VALUES('provider',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(mode,))
