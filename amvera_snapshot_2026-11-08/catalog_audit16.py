"""MEGAPATCH16A: safe read-only inventory of Jorik's separate SQLite catalogs.

The audit deliberately reports schema/counts only. It never returns user rows,
prices, tokens or other personal data and is safe to run on a local backup.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import re
import sqlite3
from pathlib import Path


DATABASES = (
    "zhorik.sqlite3",
    "jorik_recipe_catalog.sqlite3",
    "jorik_receipts.sqlite3",
    "recipe_catalog_import.sqlite3",
)
DOMAINS = {
    "prices": ("price", "offer", "sku", "retail", "product", "shop", "store"),
    "recipes": ("recipe", "ingredient", "dish", "menu"),
    "nutrition": ("calori", "nutrition", "kbju", "food"),
    "lists": ("list", "item", "shopping"),
    "other": (),
}
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*$")


def _domain(table: str) -> str:
    name = table.casefold()
    if name.startswith(('receipt_price_', 'pb_', 'zb_', 'price_', 'retail_', 'store_', 'shop_')):
        return 'prices'
    if name.startswith(('receipt_', 'mail_')):
        return 'other'
    if name.startswith(('shopping_', 'list_', 'user_list_')):
        return 'lists'
    if name.startswith(('recipe_', 'zr_', 'dish_', 'menu_')):
        return 'recipes'
    if name.startswith(('nutrition_', 'calorie_', 'kbju_', 'food_')):
        return 'nutrition'
    return "other"


def audit_file(path: Path) -> dict:
    """Read only; a malformed/corrupt/missing catalog is recorded, not edited."""
    result = {"file": path.name, "status": "missing", "tables": [], "domain_totals": {}}
    if not path.is_file():
        return result
    try:
        # mode=ro prevents creating or migrating files; immutable=1 avoids WAL
        # sidecars on copied, quiescent backup files. Never use on a live DB.
        from urllib.parse import quote
        uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
            db.execute("PRAGMA query_only=ON")
            # Probe page-level consistency; no repair is ever attempted.
            check = db.execute("PRAGMA quick_check(1)").fetchone()
            if not check or check[0] != "ok":
                result["status"] = "integrity_error"
                return result
            names = [row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            for table in names:
                if not _SAFE_NAME.fullmatch(table):
                    # Never interpolate arbitrary catalog identifiers into SQL.
                    result.setdefault("skipped_nonstandard_table_names", 0)
                    result["skipped_nonstandard_table_names"] += 1
                    continue
                count = int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                cols = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]
                domain = _domain(table)
                result["tables"].append({"table": table, "domain": domain,
                                         "rows": count, "columns": cols})
                result["domain_totals"][domain] = result["domain_totals"].get(domain, 0) + count
            result["status"] = "ok"
    except (sqlite3.Error, OSError, ValueError):
        result["status"] = "unreadable"
        result["tables"] = []
        result["domain_totals"] = {}
    return result


def audit_directory(directory: Path) -> dict:
    results = [audit_file(directory / name) for name in DATABASES]
    return {"audit": "MEGAPATCH16A", "mode": "read_only_offline",
            "files": results,
            "warnings": ["Counts are not verified market prices or canonical product counts.",
                         "No catalog merge, migration, network request or write was performed.",
                         "Only run immutable SQLite inspection on copied, stopped backups."]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline, privacy-safe Jorik catalog inventory")
    parser.add_argument("directory", type=Path, help="Folder containing COPIED SQLite backups")
    args = parser.parse_args(argv)
    report = audit_directory(args.directory)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Only the primary database is mandatory for an audit gate.
    return 0 if report['files'][0]['status'] == 'ok' else 2


if __name__ == "__main__":
    raise SystemExit(main())
