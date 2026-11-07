"""Read-only Yandex IMAP receipt sync with durable, review-only SQLite staging.

Both original OFD HTML emails and forwarded bundles of .eml attachments work.
This module never edits Zhorik's production tables or changes mail flags.
To retain the checkpoint across Amvera restarts, set JORIK_RECEIPTS_DB_PATH to
a SQLite file on persistent storage (NOT an ephemeral container directory).
"""

from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import imaplib
import json
import os
from pathlib import Path
import sqlite3
from collections import Counter
import re

from jorik_receipt_mail import (
    ReceiptCandidate, ReceiptMailError, extract_receipts_from_email,
    parse_purchase_items,
)

PARSER_REVISION = "receipt-batch-v2"


class ReceiptMailboxChanged(ReceiptMailError):
    """UID identity has changed; require deliberate reconciliation."""


def _purchase_timestamp(text: str) -> str | None:
    """Fiscal sale date, not the later forwarding/email timestamp."""
    match = re.search(r"Дата\s*\|\s*Время\s*\|?\s*(\d{2}\.\d{2}\.\d{4})\s*\|\s*(\d{2}:\d{2})",
                      text, re.IGNORECASE)
    return f"{match.group(1)} {match.group(2)}" if match else None


def _retailer_hint(text: str) -> str:
    value = text.casefold()
    for marker, retailer in (("перекресток", "Перекрёсток"), ("перекрёсток", "Перекрёсток"),
                             ("вкусвилл", "ВкусВилл"), ("пятерочка", "Пятёрочка"),
                             ("пятёрочка", "Пятёрочка"), ("магнит", "Магнит"),
                             ("дикси", "Дикси"), ("лента", "Лента")):
        if marker in value:
            return retailer
    return "не определён"


def _benefit_flag(text: str) -> str:
    value = text.casefold()
    return "possible_bonus_or_discount" if any(
        marker in value for marker in ("баллами", "бонусами", "списано баллов", "списание бонус",
                                    "скидка", "скидки", "купон", "промокод", "оплата балл")) else "not_shown"


def _init(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA busy_timeout=2000")
    db.execute("""CREATE TABLE IF NOT EXISTS mail_checkpoint (
        mailbox TEXT PRIMARY KEY, uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS mail_import_failures (
        mailbox TEXT NOT NULL, uidvalidity TEXT NOT NULL, uid INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'retry',
        PRIMARY KEY(mailbox, uidvalidity, uid)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS receipt_inbox (
        fingerprint TEXT PRIMARY KEY, mailbox TEXT NOT NULL, uid TEXT NOT NULL,
        receipt_date TEXT, receipt_total TEXT, items_json TEXT NOT NULL,
        checked INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending_review',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS receipt_personal_purchases (
        fingerprint TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
        receipt_date TEXT, receipt_total TEXT, items_json TEXT NOT NULL,
        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS receipt_price_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL,
        product_name TEXT NOT NULL, unit_price TEXT NOT NULL,
        quantity TEXT NOT NULL, unit TEXT NOT NULL, line_total TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending_review',
        UNIQUE(fingerprint, product_name, unit_price, quantity, line_total)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS receipt_price_observations (
        fingerprint TEXT NOT NULL, item_index INTEGER NOT NULL,
        product_name TEXT NOT NULL, paid_unit_price TEXT NOT NULL,
        quantity TEXT NOT NULL, unit TEXT NOT NULL, line_total TEXT NOT NULL,
        retailer_hint TEXT NOT NULL, receipt_date TEXT,
        benefit_flag TEXT NOT NULL, price_kind TEXT NOT NULL DEFAULT 'effective_receipt_price',
        review_status TEXT NOT NULL DEFAULT 'unverified',
        PRIMARY KEY(fingerprint, item_index)
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(receipt_inbox)")}
    for name in ("source_subject", "source_sender", "source_text", "parse_warning", "parser_revision"):
        if name not in columns:
            db.execute(f"ALTER TABLE receipt_inbox ADD COLUMN {name} TEXT")
    db.commit()


def _fingerprint(candidate: ReceiptCandidate) -> str:
    """Duplicate detection across repeated forwards; no raw mail stored."""
    parsed = parse_purchase_items(candidate)
    normalized = {
        "date": candidate.date.strip(),
        "total": str(parsed.total),
        "items": [(i.name, str(i.quantity), i.unit, str(i.total)) for i in parsed.items],
        "qr": list(candidate.qr_values),
    }
    # Empty, unrecognized receipts must not collide just because date is absent.
    if not parsed.items:
        normalized["unrecognized_text_hash"] = sha256(candidate.text.encode()).hexdigest()
    return sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def stage_candidates(db: sqlite3.Connection, mailbox: str, candidates: list[ReceiptCandidate]) -> tuple[int, int]:
    """Stage candidates for owner confirmation; returns inserted/duplicate counts."""
    inserted = 0
    for candidate in candidates:
        parsed = parse_purchase_items(candidate)
        items = [
            {"name": i.name, "unit_price": str(i.unit_price), "quantity": str(i.quantity),
             "unit": i.unit, "total": str(i.total)}
            for i in parsed.items
        ]
        fingerprint = _fingerprint(candidate)
        result = db.execute(
            """INSERT OR IGNORE INTO receipt_inbox
               (fingerprint, mailbox, uid, receipt_date, receipt_total, items_json, checked, status,
                source_subject, source_sender, source_text, parse_warning, parser_revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?)""",
            (fingerprint, mailbox, candidate.uid, _purchase_timestamp(candidate.text) or candidate.date,
             str(parsed.total) if parsed.total is not None else None,
             json.dumps(items, ensure_ascii=False), int(parsed.reconciled),
             candidate.subject, candidate.sender, candidate.text[:120_000], parsed.warning,
             PARSER_REVISION),
        )
        inserted += result.rowcount
        if result.rowcount:
            _stage_observations(db, fingerprint, candidate, items)
    return inserted, len(candidates) - inserted


def _stage_observations(db: sqlite3.Connection, fingerprint: str,
                        candidate: ReceiptCandidate, items: list[dict]) -> None:
    """Historical effective prices only; NEVER treated as shelf prices."""
    for idx, item in enumerate(items):
        db.execute("""INSERT OR IGNORE INTO receipt_price_observations
            (fingerprint,item_index,product_name,paid_unit_price,quantity,unit,
             line_total,retailer_hint,receipt_date,benefit_flag)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (fingerprint, idx, item['name'], item['unit_price'], item['quantity'],
             item['unit'], item['total'], _retailer_hint(candidate.text),
             _purchase_timestamp(candidate.text), _benefit_flag(candidate.text)))


def _uidvalidity(imap: imaplib.IMAP4_SSL) -> str:
    # imaplib.response returns (response-code, values), NOT ("OK", values).
    # A successful SELECT typically provides ("UIDVALIDITY", [b"12345"]).
    code, values = imap.response("UIDVALIDITY")
    if code != "UIDVALIDITY" or not values or not values[0]:
        raise ReceiptMailError("IMAP не вернул UIDVALIDITY")
    return values[0].decode("ascii") if isinstance(values[0], bytes) else str(values[0])


def _record_failure(db: sqlite3.Connection, mailbox: str, validity: str, uid: int) -> bool:
    """After 3 failures, quarantine UID instead of blocking the entire backlog.

    The quarantined UID remains visible in a separate table for manual retry.
    """
    with db:
        db.execute("""INSERT INTO mail_import_failures(mailbox, uidvalidity, uid, attempts)
            VALUES (?, ?, ?, 1) ON CONFLICT(mailbox, uidvalidity, uid)
            DO UPDATE SET attempts=attempts+1""", (mailbox, validity, uid))
        attempts = db.execute("""SELECT attempts FROM mail_import_failures
            WHERE mailbox=? AND uidvalidity=? AND uid=?""", (mailbox, validity, uid)).fetchone()[0]
        if attempts >= 3:
            db.execute("""UPDATE mail_import_failures SET status='quarantined'
                WHERE mailbox=? AND uidvalidity=? AND uid=?""", (mailbox, validity, uid))
            db.execute("""INSERT INTO mail_checkpoint(mailbox, uidvalidity, last_uid)
                VALUES (?, ?, ?) ON CONFLICT(mailbox) DO UPDATE SET
                uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid""", (mailbox, validity, uid))
            return True
    return False


def sync_once(*, limit: int = 25) -> dict[str, int]:
    """Fetch first unprocessed UIDs, process, and checkpoint atomically per mail.

    Never skips historical mail just because more than `limit` messages exist.
    If parsing fails, that UID remains pending and is retried next run.
    """
    address = os.environ.get("JORIK_RECEIPTS_EMAIL", "").strip()
    password = os.environ.get("JORIK_RECEIPTS_APP_PASSWORD", "")
    state_path = os.environ.get("JORIK_RECEIPTS_DB_PATH", "").strip() or str(
        Path(os.environ.get("DATA_DIR", "/data")) / "jorik_receipts.sqlite3"
    )
    folder = os.environ.get("JORIK_RECEIPTS_FOLDER", "INBOX").strip()
    if not address or not password:
        raise ReceiptMailError("Не заданы почтовые секреты")
    if state_path == ":memory:":
        raise ReceiptMailError("Хранилище должно сохраняться после перезапуска")
    if not folder or any(c in folder for c in ('\n', '\r', '"')):
        raise ReceiptMailError("Некорректное имя папки")
    limit = max(1, min(int(limit), 50))
    mailbox = address.lower() + "/" + folder
    counts = {"mail_scanned": 0, "receipts_staged": 0, "duplicates": 0, "errors": 0}
    imap = None
    try:
        # Separate receipts DB, never the bot's transactional SQLite connection.
        with closing(sqlite3.connect(state_path, timeout=3)) as db:
            _init(db)
            imap = imaplib.IMAP4_SSL("imap.yandex.ru", 993, timeout=15)
            imap.login(address, password)
            if imap.select('"' + folder + '"', readonly=True)[0] != "OK":
                raise ReceiptMailError("Не удалось открыть папку")
            validity = _uidvalidity(imap)
            row = db.execute("SELECT uidvalidity, last_uid FROM mail_checkpoint WHERE mailbox=?", (mailbox,)).fetchone()
            # A UID is unique only within one UIDVALIDITY generation. Reusing
            # (mailbox, uid) after a reset can hide a completely different mail;
            # replaying with parser-dependent fingerprints can create duplicates.
            # Stop without modifying the checkpoint or staging records.
            if row and row[0] != validity:
                raise ReceiptMailboxChanged("UIDVALIDITY изменился; нужен ручной разбор папки")
            last_uid = row[1] if row else 0
            status, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
            if status != "OK":
                raise ReceiptMailError("Не удалось получить UID писем")
            all_uids = sorted((int(x) for x in (data[0] or b"").split() if x.isdigit()))
            for uid in (x for x in all_uids if x > last_uid):
                if counts["mail_scanned"] >= limit:
                    break
                status, parts = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    counts["errors"] += 1
                    if _record_failure(db, mailbox, validity, uid):
                        counts["mail_scanned"] += 1
                        continue
                    break  # retry this UID on next run
                raw = next((p[1] for p in parts if isinstance(p, tuple) and isinstance(p[1], bytes)), None)
                if not raw:
                    counts["errors"] += 1
                    if _record_failure(db, mailbox, validity, uid):
                        counts["mail_scanned"] += 1
                        continue
                    break
                try:
                    candidates = extract_receipts_from_email(str(uid), raw)
                    with db:
                        added, duplicated = stage_candidates(db, mailbox, candidates)
                        db.execute("""INSERT INTO mail_checkpoint(mailbox, uidvalidity, last_uid)
                            VALUES (?, ?, ?) ON CONFLICT(mailbox) DO UPDATE SET
                            uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid""",
                            (mailbox, validity, uid))
                except (ReceiptMailError, sqlite3.Error):
                    counts["errors"] += 1
                    if _record_failure(db, mailbox, validity, uid):
                        counts["mail_scanned"] += 1
                        continue
                    break
                counts["mail_scanned"] += 1
                counts["receipts_staged"] += added
                counts["duplicates"] += duplicated
    except (imaplib.IMAP4.error, OSError, sqlite3.Error) as exc:
        raise ReceiptMailError("Ошибка почты или хранилища; проверьте настройки") from exc
    finally:
        if imap is not None:
            try:
                imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    return counts


def _state_db() -> sqlite3.Connection:
    path = os.environ.get("JORIK_RECEIPTS_DB_PATH", "").strip() or str(
        Path(os.environ.get("DATA_DIR", "/data")) / "jorik_receipts.sqlite3"
    )
    if not Path(path).exists():
        raise ReceiptMailError("Импорт ещё не запускался: база чеков отсутствует")
    return sqlite3.connect(path, timeout=3)


def list_pending(limit: int = 10, offset: int = 0) -> list[dict]:
    with closing(_state_db()) as db:
        _init(db)
        rows = db.execute("""SELECT fingerprint, receipt_date, receipt_total,
                            items_json, checked, uid, source_subject, source_sender, parse_warning
                            FROM receipt_inbox
                            WHERE status='pending_review' ORDER BY created_at DESC, rowid DESC
                            LIMIT ? OFFSET ?""", (min(max(1, limit), 30),
                                                   min(max(0, offset), 100000))).fetchall()
    return [{"key": r[0][:12], "date": r[1], "total": r[2],
             "items": json.loads(r[3]), "reconciled": bool(r[4]),
             "uid": r[5], "subject": r[6], "sender": r[7], "warning": r[8]} for r in rows]


def receipt_diagnostics() -> dict:
    """Aggregate metadata only; no personal purchases or mail text exported."""
    with closing(_state_db()) as db:
        _init(db)
        rows = db.execute("""SELECT receipt_total, items_json, checked, source_text, parser_revision
            FROM receipt_inbox WHERE status='pending_review'""").fetchall()
        sample = db.execute("""SELECT substr(fingerprint,1,12), uid, receipt_date,
            receipt_total, items_json, checked, source_text FROM receipt_inbox
            WHERE status='pending_review' ORDER BY rowid DESC LIMIT 500""").fetchall()
    counts = Counter()
    for total, items_json, checked, source_text, parser_revision in rows:
        counts['pending'] += 1
        counts['reconciled' if checked else 'unreconciled'] += 1
        if total is None:
            counts['no_total'] += 1
        if not json.loads(items_json):
            counts['no_items'] += 1
        if source_text is None:
            counts['legacy_no_source'] += 1
        if parser_revision != PARSER_REVISION:
            counts['needs_reparse'] += 1
    examples = [{"key": r[0], "uid": r[1], "date": r[2], "total": r[3],
                 "items": len(json.loads(r[4])), "ok": bool(r[5]),
                 "source_available": r[6] is not None} for r in sample]
    return {"counts": dict(counts), "examples": examples}


def pending_receipt_detail(key: str) -> dict | None:
    if len(key) != 12 or any(c not in '0123456789abcdef' for c in key):
        return None
    with closing(_state_db()) as db:
        _init(db)
        rows = db.execute("""SELECT receipt_date, receipt_total, items_json, checked,
            uid, source_subject, source_sender, source_text, parse_warning
            FROM receipt_inbox WHERE substr(fingerprint,1,12)=?
            AND status='pending_review'""", (key,)).fetchall()
    if len(rows) != 1:
        return None
    r = rows[0]
    return {"key": key, "date": r[0], "total": r[1], "items": json.loads(r[2]),
            "reconciled": bool(r[3]), "uid": r[4], "subject": r[5],
            "sender": r[6], "source_text": r[7], "warning": r[8]}


def reparse_legacy_receipts(*, limit: int = 10) -> dict[str, int]:
    """Refetch old UIDs read-only, retain fingerprints, decisions and scan cursor.

    Only older parser revisions of pending receipts are refreshed. No new rows and
    no production price writes. If UIDVALIDITY changed, refuse to guess UIDs.
    """
    address = os.environ.get("JORIK_RECEIPTS_EMAIL", "").strip()
    password = os.environ.get("JORIK_RECEIPTS_APP_PASSWORD", "")
    folder = os.environ.get("JORIK_RECEIPTS_FOLDER", "INBOX").strip()
    if not address or not password or not folder or any(c in folder for c in '\r\n"'):
        raise ReceiptMailError("Почтовые настройки отсутствуют или неверны")
    mailbox = address.lower() + "/" + folder
    counts = {"emails": 0, "updated": 0, "missing": 0, "errors": 0, "ambiguous": 0}
    imap = None
    try:
        with closing(_state_db()) as db:
            _init(db)
            checkpoint = db.execute("SELECT uidvalidity FROM mail_checkpoint WHERE mailbox=?", (mailbox,)).fetchone()
            if not checkpoint:
                raise ReceiptMailError("Не найден контрольный курсор почты")
            targets = db.execute("""SELECT fingerprint, uid FROM receipt_inbox
                WHERE mailbox=? AND status='pending_review'
                  AND (parser_revision IS NULL OR parser_revision != ?)
                ORDER BY rowid DESC""", (mailbox, PARSER_REVISION)).fetchall()
            by_uid = {}
            for fingerprint, receipt_uid in targets:
                base = receipt_uid.split(':', 1)[0]
                if base.isdigit():
                    by_uid.setdefault(int(base), {}).setdefault(receipt_uid, []).append(fingerprint)
            imap = imaplib.IMAP4_SSL("imap.yandex.ru", 993, timeout=15)
            imap.login(address, password)
            if imap.select('"' + folder + '"', readonly=True)[0] != 'OK':
                raise ReceiptMailError("Не удалось открыть папку IMAP")
            if _uidvalidity(imap) != checkpoint[0]:
                raise ReceiptMailError("UIDVALIDITY изменился — повторный разбор остановлен")
            for uid in list(by_uid)[:max(1, min(limit, 25))]:
                counts['emails'] += 1
                status, chunks = imap.uid('fetch', str(uid), '(BODY.PEEK[])')
                raw = next((p[1] for p in chunks or [] if isinstance(p, tuple) and
                            len(p) > 1 and isinstance(p[1], bytes)), None)
                if status != 'OK' or raw is None:
                    counts['missing'] += 1
                    continue
                try:
                    candidates = extract_receipts_from_email(str(uid), raw)
                    matched = set()
                    with db:
                        for candidate in candidates:
                            # Complete UID (e.g. 5:28) is stable within an
                            # unchanged UIDVALIDITY. A parser fix can change
                            # the fingerprint, so never rely on that hash.
                            possible = by_uid[uid].get(candidate.uid, [])
                            if not possible:
                                continue
                            if len(possible) != 1:
                                counts['ambiguous'] += len(possible)
                                continue
                            fingerprint = possible[0]
                            parsed = parse_purchase_items(candidate)
                            items = [{"name": i.name, "unit_price": str(i.unit_price),
                                      "quantity": str(i.quantity), "unit": i.unit,
                                      "total": str(i.total)} for i in parsed.items]
                            result = db.execute("""UPDATE receipt_inbox SET
                                receipt_date=?, receipt_total=?, items_json=?, checked=?, source_subject=?,
                                source_sender=?, source_text=?, parse_warning=?, parser_revision=?
                                WHERE fingerprint=? AND status='pending_review'
                                  AND (parser_revision IS NULL OR parser_revision != ?)""",
                                (_purchase_timestamp(candidate.text) or candidate.date,
                                 str(parsed.total) if parsed.total is not None else None,
                                 json.dumps(items, ensure_ascii=False), int(parsed.reconciled),
                                 candidate.subject, candidate.sender, candidate.text[:120_000],
                                 parsed.warning, PARSER_REVISION, fingerprint, PARSER_REVISION))
                            counts['updated'] += result.rowcount
                            if result.rowcount:
                                db.execute("DELETE FROM receipt_price_observations WHERE fingerprint=?", (fingerprint,))
                                _stage_observations(db, fingerprint, candidate, items)
                            matched.add(candidate.uid)
                    counts['missing'] += sum(len(v) for k, v in by_uid[uid].items()
                                             if k not in matched)
                except (ReceiptMailError, ValueError, sqlite3.Error):
                    counts['errors'] += 1
    except (imaplib.IMAP4.error, OSError, sqlite3.Error) as exc:
        raise ReceiptMailError("Не удалось повторно прочитать почту") from exc
    finally:
        if imap is not None:
            try:
                imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    return counts


def receipt_stats() -> dict[str, int]:
    with closing(_state_db()) as db:
        _init(db)
        result = dict(db.execute("SELECT status, count(*) FROM receipt_inbox GROUP BY status").fetchall())
        result["quarantined"] = db.execute("""SELECT count(*) FROM mail_import_failures
            WHERE status='quarantined'""").fetchone()[0]
        result["price_observations"] = db.execute(
            "SELECT count(*) FROM receipt_price_observations").fetchone()[0]
        result["benefit_suspected"] = db.execute("""SELECT count(*) FROM receipt_price_observations
            WHERE benefit_flag='possible_bonus_or_discount'""").fetchone()[0]
        return result


def review_receipt(key: str, *, owner_id: int, approve: bool) -> str:
    """Explicit owner action. No mutation of bot's shared prices/product tables."""
    if len(key) != 12 or not all(c in "0123456789abcdef" for c in key):
        raise ReceiptMailError("Неверный идентификатор чека")
    with closing(_state_db()) as db:
        _init(db)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                matches = db.execute("""SELECT fingerprint, receipt_date, receipt_total, items_json, checked
                    FROM receipt_inbox WHERE substr(fingerprint,1,12)=? AND status='pending_review'""", (key,)).fetchall()
                if len(matches) != 1:
                    return "Чек уже обработан или не найден"
                fingerprint, date, total, payload, reconciled = matches[0]
                if approve and not reconciled:
                    return "Сумма чека не сошлась — автоматическое подтверждение запрещено"
                if approve:
                    db.execute("""INSERT OR IGNORE INTO receipt_personal_purchases
                        (fingerprint, owner_id, receipt_date, receipt_total, items_json)
                        VALUES (?, ?, ?, ?, ?)""", (fingerprint, owner_id, date, total, payload))
                    for item in json.loads(payload):
                        db.execute("""INSERT OR IGNORE INTO receipt_price_candidates
                            (fingerprint, product_name, unit_price, quantity, unit, line_total)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                            (fingerprint, item["name"], item["unit_price"], item["quantity"], item["unit"], item["total"]))
                db.execute("UPDATE receipt_inbox SET status=? WHERE fingerprint=?",
                           ("confirmed" if approve else "rejected", fingerprint))
                return "Чек подтверждён; цены ждут отдельной проверки" if approve else "Чек отклонён"
        except sqlite3.Error as exc:
            raise ReceiptMailError("Не удалось сохранить решение по чеку") from exc


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Импорт чеков Жорика без записи в рабочие цены")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()
    try:
        print(json.dumps(sync_once(limit=args.limit), ensure_ascii=False))
        return 0
    except ReceiptMailError:
        print("Импорт не выполнен: проверьте IMAP, секреты и постоянный путь к БД")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
