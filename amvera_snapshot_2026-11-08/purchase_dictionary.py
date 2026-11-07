"""Independent, conservative dictionary of things people may buy.

No prices, menus, recipes, or Telegram. Unknown names are never rejected.
Custom synonyms are stored in a separate SQLite database under DATA_DIR.
"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
import os
import re
import sqlite3


@dataclass(frozen=True)
class PurchaseTerm:
    name: str
    category: str
    unit: str = "шт."


_SEEDS = {
    "молоко": ("Молоко", "Молочные продукты", "пак."),
    "молочка": ("Молоко", "Молочные продукты", "пак."),
    "молочка пакет": ("Молоко", "Молочные продукты", "пак."),
    "пиво": ("Пиво", "Напитки", "бут."),
    "пивка": ("Пиво", "Напитки", "бут."),
    "водка": ("Водка", "Напитки", "бут."),
    "водочки": ("Водка", "Напитки", "бут."),
    "водку": ("Водка", "Напитки", "бут."),
    "туалетка": ("Туалетная бумага", "Хозяйственные товары", "упак."),
    "туалетная бумага": ("Туалетная бумага", "Хозяйственные товары", "упак."),
    "бумажные полотенца": ("Бумажные полотенца", "Хозяйственные товары", "упак."),
    "полотенца бумажные": ("Бумажные полотенца", "Хозяйственные товары", "упак."),
    "спальник": ("Спальный мешок", "Туризм", "шт."),
    "спальный мешок": ("Спальный мешок", "Туризм", "шт."),
    "корм для попугаев": ("Корм для попугаев", "Товары для животных", "упак."),
    "корм для кошки": ("Корм для кошек", "Товары для животных", "упак."),
    "спички": ("Спички", "Хозяйственные товары", "упак."),
    "дрова": ("Дрова", "Дача и отдых", "упак."),
}

_QUANTITY = re.compile(
    r"\b(?:(\d+(?:[.,]\d+)?)|(пару|пара|две|два|один|одну|одна|три))\s*"
    r"(пакет(?:а|ов)?|упаков(?:ка|ки|ок|ку)|бутыл(?:ка|ки|ок|ку)|штук(?:и)?|шт)\b", re.I)
_PREFIX_UNIT = re.compile(r"^(пакет|упаковку|бутылку|бутылка)\s+", re.I)
_BARE_UNIT = re.compile(r"\s+(бутылку|бутылка|пакет|упаковку)\s*$", re.I)
_NUMBERS = {"пару": 2, "пара": 2, "две": 2, "два": 2, "один": 1,
            "одну": 1, "одна": 1, "три": 3}
_ACTION = re.compile(r"^(?:позвонить|написать|взять|проверить|предупредить)\b", re.I)


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().strip(" .,:;!«»"))


def _connect(path: str | Path | None = None):
    dest = Path(path) if path is not None else Path(os.getenv("DATA_DIR", "/data")) / "jorik_purchase_terms.sqlite3"
    dest.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(dest, timeout=2)
    db.execute("PRAGMA busy_timeout=2000")
    db.execute("""CREATE TABLE IF NOT EXISTS purchase_terms (
      phrase TEXT PRIMARY KEY, canonical TEXT NOT NULL,
      category TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'шт.')""")
    return db


def add_alias(phrase: str, name: str, category: str, unit: str = "шт.", *, db_path=None):
    """Explicitly approved synonym; never auto-promote user utterances."""
    if not all(v and len(v.strip()) <= 120 for v in (phrase, name, category, unit)):
        raise ValueError("Invalid dictionary entry")
    with closing(_connect(db_path)) as db:
        with db:
            db.execute("""INSERT INTO purchase_terms(phrase,canonical,category,unit)
              VALUES(?,?,?,?) ON CONFLICT(phrase) DO UPDATE SET
              canonical=excluded.canonical,category=excluded.category,unit=excluded.unit""",
              (_key(phrase), name.strip(), category.strip(), unit.strip()))


def lookup(phrase: str, *, db_path=None) -> PurchaseTerm | None:
    key = _key(phrase)
    if not key:
        return None
    # Custom aliases may override bundled starter vocabulary.
    with closing(_connect(db_path)) as db:
        row = db.execute("SELECT canonical,category,unit FROM purchase_terms WHERE phrase=?", (key,)).fetchone()
    term = row or _SEEDS.get(key)
    return PurchaseTerm(*term) if term else None


def suggest_item(text: str, *, db_path=None) -> tuple[str, str | None]:
    """Returns (safe text for existing list preview, category or None).

    Unrecognized or action-like phrases are returned unchanged.
    """
    original = (text or "").strip()
    if not original or _ACTION.match(original):
        return original, None
    quantity = _QUANTITY.search(original)
    remainder = (original[:quantity.start()] + " " + original[quantity.end():]).strip() if quantity else original
    prefix = _PREFIX_UNIT.match(remainder)
    if prefix:
        remainder = remainder[prefix.end():].strip()
    suffix = _BARE_UNIT.search(remainder) if not quantity else None
    if suffix:
        remainder = remainder[:suffix.start()].strip()
    term = lookup(remainder, db_path=db_path)
    if term is None:
        return original, None
    if quantity:
        number = quantity.group(1) or str(_NUMBERS[quantity.group(2).casefold()])
        unit = quantity.group(3).casefold()
        rendered_unit = "бут." if unit.startswith("бутыл") else "пак." if unit.startswith(("пакет", "упаков")) else "шт."
        return f"{term.name} {number} {rendered_unit}", term.category
    if prefix or suffix:
        unit = "бут." if (prefix or suffix).group(1).casefold().startswith("бутыл") else "пак."
        return f"{term.name} 1 {unit}", term.category
    return term.name, term.category


def suggest_list(text: str, *, db_path=None) -> tuple[str, list[tuple[str, str, str | None]]]:
    """Only explicitly delimited items are rewritten; mixed speech is preserved."""
    chunks = re.split(r"([,;\n])", text or "")
    if len(chunks) == 1:
        before = (text or "").strip()
        after, category = suggest_item(before, db_path=db_path)
        return after, [(before, after, category)] if after != before else []
    changes = []
    for i in range(0, len(chunks), 2):
        before = chunks[i].strip()
        if not before:
            continue
        after, category = suggest_item(before, db_path=db_path)
        if after != before:
            changes.append((before, after, category))
            leading = re.match(r"\s*", chunks[i]).group()
            chunks[i] = leading + after
    return "".join(chunks), changes
