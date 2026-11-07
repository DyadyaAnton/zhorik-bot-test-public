"""Authoritative per-user weekly planning budget.

Schema belongs to startup, never to a hot UI read path. Legacy menus are
consulted only when the user has not yet explicitly set a weekly budget.
"""

import math
import sqlite3
from database import connect_db


def init():
    with connect_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS zm_menu_budget (
            user_id INTEGER PRIMARY KEY,
            budget REAL NOT NULL CHECK(budget >= 100 AND budget <= 1000000))''')
        db.execute('''CREATE TABLE IF NOT EXISTS zm_menu_people (
            user_id INTEGER PRIMARY KEY,
            people INTEGER NOT NULL CHECK(people BETWEEN 1 AND 3))''')


def get(uid):
    # Read-only path: schema creation belongs to init().
    try:
        with connect_db() as db:
            row=db.execute('SELECT budget FROM zm_menu_budget WHERE user_id=?',(uid,)).fetchone()
            if row is None:
                row=db.execute('SELECT budget FROM menus WHERE owner_id=? AND budget>0 ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table: zm_menu_budget' not in str(exc):raise
        # Legacy or damaged installations: do not recreate tables during reads.
        with connect_db() as db:
            row=db.execute('SELECT budget FROM menus WHERE owner_id=? AND budget>0 ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
    if row is None:return None
    try:value=float(row['budget'])
    except (TypeError,ValueError,OverflowError):return None
    return value if math.isfinite(value) and 100<=value<=1000000 else None


def get_people(uid):
    with connect_db() as db:
        row=db.execute('SELECT people FROM zm_menu_people WHERE user_id=?',(uid,)).fetchone()
    try:
        people=int(row['people']) if row else 1
    except (ValueError,TypeError,OverflowError):return 1
    return people if people in (1,2,3) else 1


def set_people(uid, people):
    if type(people) is not int or people not in (1,2,3):
        raise ValueError('Количество человек: от 1 до 3')
    with connect_db() as db:
        db.execute('''INSERT INTO zm_menu_people(user_id,people) VALUES(?,?)
          ON CONFLICT(user_id) DO UPDATE SET people=excluded.people
          WHERE zm_menu_people.people!=excluded.people''',(uid,people))


def set_budget(uid,value):
    try:value=float(value)
    except (TypeError,ValueError,OverflowError):raise ValueError('Неверный бюджет') from None
    if not math.isfinite(value) or not 100<=value<=1000000:
        raise ValueError('Бюджет должен быть от 100 до 1 000 000 рублей')
    statement='''INSERT INTO zm_menu_budget(user_id,budget) VALUES(?,?)
                 ON CONFLICT(user_id) DO UPDATE SET budget=excluded.budget
                 WHERE zm_menu_budget.budget!=excluded.budget'''
    try:
        with connect_db() as db:db.execute(statement,(uid,value))
    except sqlite3.OperationalError as exc:
        if 'no such table: zm_menu_budget' not in str(exc):raise
        # Defensive first-write recovery for isolated tests or an old schema.
        # Normal production startup has already called init(). Reads never DDL.
        init()
        with connect_db() as db:db.execute(statement,(uid,value))
