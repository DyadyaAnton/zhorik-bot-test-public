"""Read-only pilot importer for receipt emails in a dedicated Yandex mailbox.

No bot/SQLite integration: the caller reviews parsed receipts before saving.
Uses Python's standard library only. Never logs credentials or message bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
from decimal import Decimal
from email import policy
from email.message import Message
from email.parser import BytesParser
from html import unescape
from html.parser import HTMLParser
import imaplib
import os
import re


class ReceiptMailError(RuntimeError):
    """Mailbox or parsing failure safe to report without secrets."""


@dataclass(frozen=True)
class ReceiptCandidate:
    uid: str
    message_id: str
    sender: str
    subject: str
    date: str
    text: str
    qr_values: tuple[str, ...]


@dataclass(frozen=True)
class ReceiptItem:
    name: str
    unit_price: Decimal
    quantity: Decimal
    unit: str
    total: Decimal


@dataclass(frozen=True)
class ParsedReceipt:
    items: tuple[ReceiptItem, ...]
    total: Decimal | None
    reconciled: bool
    warning: str | None


class _TextFromHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script"):
            self.skip += 1
        if tag in ("br", "p", "tr", "li", "div"):
            self.parts.append("\n")
        if tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self.skip:
            self.skip -= 1
        if tag in ("p", "tr", "li", "div"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.parts.append(data)

    def result(self) -> str:
        return unescape("".join(self.parts))


_QR = re.compile(
    r"(?:^|[?&\s])t=\d{8}T\d{4}(?:\d{2})?&s=\d+(?:\.\d{1,2})?&fn=\d+&i=\d+&fp=\d+&n=\d+",
    re.IGNORECASE,
)


def _body_text(message: Message, max_chars: int = 120_000) -> str:
    """Extract text/HTML without downloading or executing external URLs."""
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            continue
        if not isinstance(value, str):
            continue
        if content_type == "text/plain":
            plain.append(value)
        else:
            parser = _TextFromHTML()
            parser.feed(value)
            html.append(parser.result())
    plain_text = "\n".join(plain)
    html_text = "\n".join(html)
    # Many OFDs attach both a short plaintext notification and a full HTML
    # fiscal table. Prefer whichever actually contains the itemized prices.
    def has_price_rows(value: str) -> bool:
        # \s matches normal spaces, NBSP (\u00a0), newlines, and tabs.
        return bool(re.search(r"Цена\s*\*\s*Кол", value, re.IGNORECASE))
    if html_text and (has_price_rows(html_text) and not has_price_rows(plain_text)):
        chosen = html_text
    elif plain_text and (has_price_rows(plain_text) and not has_price_rows(html_text)):
        chosen = plain_text
    else:
        chosen = html_text if len(html_text) > len(plain_text) else plain_text
    return chosen[:max_chars]


def _decode_encoded_eml(raw: bytes) -> bytes | None:
    """Accept base64 only if it decodes to a plausible complete RFC822 message."""
    stripped = re.sub(rb"\s+", b"", raw)
    if not stripped or len(stripped) > 2_000_000 or not re.fullmatch(rb"[A-Za-z0-9+/]*={0,2}", stripped):
        return None
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(decoded) > 2_000_000:
        return None
    header = decoded.split(b"\n\n", 1)[0].split(b"\r\n\r\n", 1)[0][:16000]
    if b"\n" not in header or not re.search(
            rb"(?im)^(?:received|from|subject|date|mime-version|content-type):", header):
        return None
    return decoded


def parse_receipt_email(uid: str, raw: bytes) -> ReceiptCandidate:
    """Parse a fetched email as an untrusted candidate (not a verified receipt)."""
    if len(raw) > 2_000_000:
        raise ReceiptMailError("Письмо превышает лимит 2 МБ")
    # Yandex can forward a complete EML as base64, or wrap that base64
    # in the text body of an outer message/rfc822 part.
    for _ in range(2):
        decoded = _decode_encoded_eml(raw)
        if decoded is None:
            break
        raw = decoded
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body = _body_text(msg)
    decoded_body = _decode_encoded_eml(body.encode('ascii', 'ignore'))
    if decoded_body is not None:
        msg = BytesParser(policy=policy.default).parsebytes(decoded_body)
        body = _body_text(msg)
    return ReceiptCandidate(
        uid=uid,
        message_id=str(msg.get("Message-ID", ""))[:256],
        sender=str(msg.get("From", ""))[:512],
        subject=str(msg.get("Subject", ""))[:512],
        date=str(msg.get("Date", ""))[:128],
        text=body,
        qr_values=tuple(dict.fromkeys(v.strip() for v in _QR.findall(body))),
    )


_ITEM_HEADER = re.compile(r"^\|?\s*(\d+)\.\s*\|\s*(.+?)\s*$")
_ITEM_PRICE = re.compile(
    r"Цена\s*\*\s*Кол\s*\|\s*(\d+[.,]\d{1,2})\s*\*\s*"
    r"(\d+(?:[.,]\d+)?)\s*(шт\.|кг|г|л|мл)?\s*=\s*(\d+[.,]\d{1,2})",
    re.IGNORECASE,
)
_MONEY = re.compile(r"^\|?\s*(\d+[.,]\d{1,2})\s*$")
_TOTAL_ON_LINE = re.compile(r"^\|?\s*Итог\s*\|\s*(\d+[.,]\d{1,2})\s*\|?\s*$", re.IGNORECASE)
_TOTAL_LABEL = re.compile(r"^\|?\s*Итог\s*\|?\s*$", re.IGNORECASE)


def parse_purchase_items(candidate: ReceiptCandidate) -> ParsedReceipt:
    """Extract verified-looking line items from one known fiscal HTML layout.

    A mismatch means human review, not automatic adjustment of amounts.
    Unrecognized layouts deliberately produce an empty, unverified result.
    """
    lines = [re.sub(r"\s+", " ", line).strip() for line in candidate.text.splitlines()]
    items: list[ReceiptItem] = []
    pending_name: str | None = None
    receipt_total: Decimal | None = None
    awaiting_total = 0
    for line in lines:
        inline_total = _TOTAL_ON_LINE.fullmatch(line)
        if inline_total:
            receipt_total = Decimal(inline_total.group(1).replace(",", "."))
            awaiting_total = 0
            continue
        if _TOTAL_LABEL.fullmatch(line):
            awaiting_total = 3
            continue
        if awaiting_total:
            match_total = _MONEY.fullmatch(line)
            if match_total:
                receipt_total = Decimal(match_total.group(1).replace(",", "."))
                awaiting_total = 0
                continue
            awaiting_total -= 1
            if line.strip("| "):
                awaiting_total = 0
        match_item = _ITEM_HEADER.fullmatch(line)
        if match_item:
            pending_name = match_item.group(2).strip()
            continue
        match_price = _ITEM_PRICE.search(line)
        if pending_name and match_price:
            price, quantity, unit, total = match_price.groups()
            items.append(ReceiptItem(
                name=pending_name,
                unit_price=Decimal(price.replace(",", ".")),
                quantity=Decimal(quantity.replace(",", ".")),
                unit=unit or "шт.",
                total=Decimal(total.replace(",", ".")),
            ))
            pending_name = None
    # A matching grand total alone is not enough: malformed or misread
    # price/quantity pairs must not be confirmed as valid observations.
    lines_consistent = all(
        abs(item.unit_price * item.quantity - item.total) <= Decimal("0.01")
        for item in items
    )
    reconciled = bool(items) and lines_consistent and receipt_total is not None and sum(
        (item.total for item in items), Decimal("0")
    ) == receipt_total
    warning = None if reconciled else "Товары, арифметика строк или итог не совпадают: нужна ручная проверка"
    return ParsedReceipt(tuple(items), receipt_total, reconciled, warning)


def extract_receipts_from_email(uid: str, raw: bytes, *, max_nested: int = 400) -> list[ReceiptCandidate]:
    """Unpack forwarded .eml attachments into individual receipt candidates.

    Handles message/rfc822 MIME parts and attached *.eml octet streams.
    One parent message can contain many receipts, as in Yandex forward-as-attachment.
    """
    if len(raw) > 32_000_000:
        raise ReceiptMailError("Письмо превышает лимит 32 МБ")
    parent = BytesParser(policy=policy.default).parsebytes(raw)
    nested: list[ReceiptCandidate] = []
    for part in parent.walk():
        # The parent.walk() can also visit the inner email's body. Only
        # attachment containers should be extracted as separate receipts.
        is_rfc822 = part.get_content_type() == "message/rfc822"
        is_eml = (part.get_filename() or "").lower().endswith(".eml")
        if not (is_rfc822 or is_eml):
            continue
        if len(nested) >= max_nested:
            raise ReceiptMailError("Слишком много вложенных чеков в одном письме")
        try:
            if is_rfc822:
                payload = part.get_payload()
                inner_raw = payload[0].as_bytes(policy=policy.default) if isinstance(payload, list) and payload else b""
            else:
                inner_raw = part.get_payload(decode=True) or b""
            if inner_raw:
                nested.append(parse_receipt_email(f"{uid}:{len(nested) + 1}", inner_raw))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ReceiptMailError("Не удалось прочитать вложенный EML") from exc
    return nested or [parse_receipt_email(uid, raw)]


def fetch_candidates(*, since_uid: int = 0, limit: int = 20) -> list[ReceiptCandidate]:
    """Read recent emails. The caller must persist last UID after review.

    Required env: JORIK_RECEIPTS_EMAIL, JORIK_RECEIPTS_APP_PASSWORD.
    Optional: JORIK_RECEIPTS_FOLDER (default INBOX). No deletes, moves or flags.
    """
    address = os.environ.get("JORIK_RECEIPTS_EMAIL", "").strip()
    password = os.environ.get("JORIK_RECEIPTS_APP_PASSWORD", "")
    folder = os.environ.get("JORIK_RECEIPTS_FOLDER", "INBOX").strip()
    if not address or not password:
        raise ReceiptMailError("Не заданы почтовый адрес и пароль приложения")
    if not folder or any(char in folder for char in ('\r', '\n', '"')):
        raise ReceiptMailError("Недопустимое название папки")
    limit = max(1, min(limit, 50))
    connection: imaplib.IMAP4_SSL | None = None
    try:
        connection = imaplib.IMAP4_SSL("imap.yandex.ru", 993, timeout=15)
        connection.login(address, password)
        status, _ = connection.select(f'"{folder}"', readonly=True)
        if status != "OK":
            raise ReceiptMailError("Не удалось открыть папку с чеками")
        status, data = connection.uid("search", None, f"UID {max(1, since_uid + 1)}:*")
        if status != "OK":
            raise ReceiptMailError("Не удалось получить список писем")
        uids = (data[0] or b"").split()
        result = []
        for uid in uids[-limit:]:
            if not uid.isdigit() or int(uid) <= since_uid:
                continue
            status, chunks = connection.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw = next((part[1] for part in chunks if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
            if raw:
                result.extend(extract_receipts_from_email(uid.decode("ascii"), raw))
        return result
    except (imaplib.IMAP4.error, OSError) as exc:
        raise ReceiptMailError("Не удалось прочитать почту; проверьте IMAP и секреты") from exc
    finally:
        if connection is not None:
            try:
                connection.logout()
            except (imaplib.IMAP4.error, OSError):
                pass


def diagnostic_summary(candidates: list[ReceiptCandidate]) -> dict[str, object]:
    """Privacy-safe counters for a read-only smoke test on Amvera."""
    receipts = [parse_purchase_items(candidate) for candidate in candidates]
    return {
        "emails_or_nested_eml": len(candidates),
        "receipts_with_items": sum(bool(receipt.items) for receipt in receipts),
        "reconciled_receipts": sum(receipt.reconciled for receipt in receipts),
        "total_product_lines": sum(len(receipt.items) for receipt in receipts),
        "unrecognized_or_mismatched": sum(not receipt.reconciled for receipt in receipts),
    }


def main() -> int:
    """Run `python jorik_receipt_mail.py --check` on an Amvera shell.

    This does not change mail flags, write to SQLite, or reveal secrets.
    """
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Проверка почты чеков Жорика (только чтение)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--check", action="store_true", help="Проверить подключение IMAP")
    source.add_argument("--eml", type=Path, help="Проверить локальный файл EML без почты")
    parser.add_argument("--limit", type=int, default=20, help="Максимум писем, 1–50")
    args = parser.parse_args()
    try:
        if args.check:
            candidates = fetch_candidates(limit=args.limit)
        else:
            candidates = extract_receipts_from_email("local", args.eml.read_bytes())
        print(json.dumps(diagnostic_summary(candidates), ensure_ascii=False))
        return 0
    except (ReceiptMailError, OSError) as exc:
        # Never print IMAP exceptions containing server responses or secrets.
        print("Проверка не прошла: проверьте секреты, IMAP и доступность почты")
        return 1


__all__ = ["ReceiptCandidate", "ReceiptItem", "ParsedReceipt", "ReceiptMailError", "fetch_candidates", "parse_receipt_email", "extract_receipts_from_email", "parse_purchase_items", "diagnostic_summary"]


if __name__ == "__main__":
    raise SystemExit(main())
