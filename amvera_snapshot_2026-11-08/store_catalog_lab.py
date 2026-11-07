"""Owner-only, read-only official store URL probe and batch export.
No private APIs, no bypass, no writes to production price tables.
"""
import asyncio
import csv
import io
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, urljoin
from xml.etree import ElementTree as ET

import requests
from aiogram import F, Router
from aiogram.filters import Command, BaseFilter
from aiogram.types import BufferedInputFile, Message
from config import settings
from lab_run_guard import running_chat_ids as _running

router = Router(name="store_catalog_lab")
log = logging.getLogger(__name__)
ROOT = Path(settings.data_dir) / "store_catalog_lab"
HOSTS = {"magnit": ("magnit.ru", "www.magnit.ru"),
         "pyaterochka": ("5ka.ru", "www.5ka.ru")}
MAX_URLS = 50
MAX_BYTES = 64_000
_awaiting = set()


def is_owner(m):
    return bool(m.from_user and m.from_user.id == settings.sandbox_owner_id and m.chat.type == "private")


def validate_url(url, store=None):
    p = urlparse(url.strip())
    if p.scheme != "https" or p.username or p.password or p.port or p.fragment:
        raise ValueError("Нужна обычная HTTPS-ссылка на официальный каталог")
    host = (p.hostname or "").lower()
    match = next((s for s, hosts in HOSTS.items() if host in hosts), None)
    if not match or (store and match != store):
        raise ValueError("Допустимы только официальные сайты magnit.ru и 5ka.ru")
    return match, url.strip()


class LDParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_ld = False
        self.parts = []
        self.blocks = []
    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.in_ld = True
            self.parts = []
    def handle_data(self, data):
        if self.in_ld:
            self.parts.append(data)
    def handle_endtag(self, tag):
        if tag == "script" and self.in_ld:
            self.blocks.append("".join(self.parts))
            self.in_ld = False


def walk_products(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from walk_products(x)
    elif isinstance(obj, dict):
        kind = obj.get("@type", "")
        if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
            yield obj
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in obj:
                yield from walk_products(obj[key])


def extract_offers(html, store, url):
    parser = LDParser()
    parser.feed(html)
    output = []
    seen = set()
    for block in parser.blocks:
        try:
            data = json.loads(block)
        except (ValueError, TypeError):
            continue
        for product in walk_products(data):
            offers = product.get("offers", [])
            if isinstance(offers, dict):
                offers = [offers]
            if not isinstance(offers, list):
                continue
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                raw_price = offer.get("price") or offer.get("lowPrice")
                try:
                    price = Decimal(str(raw_price).replace(",", "."))
                except (InvalidOperation, TypeError):
                    continue
                currency = offer.get("priceCurrency", "RUB")
                if price <= 0 or price > 1000000 or currency not in ("RUB", "RUR", "643"):
                    continue
                title = str(product.get("name") or "").strip()[:300]
                if not title:
                    continue
                product_url = offer.get("url") or product.get("url") or url
                try:
                    validate_url(product_url, store)
                except (ValueError, TypeError):
                    product_url = url
                key = (title, str(price), product_url)
                if key in seen:
                    continue
                seen.add(key)
                output.append({"source": store, "url": product_url, "fetched_at": datetime.now(timezone.utc).isoformat(),
                               "region": "не подтверждён", "raw_title": title, "price": str(price),
                               "qty": "", "unit": "", "confidence": "structured_product_offer"})
    return output[:500]


def fetch_url(url):
    store, url = validate_url(url)
    # requests follows redirects by default; validate every redirect to prevent SSRF.
    with requests.Session() as session:
        current = url
        for _ in range(4):
            response = session.get(current, timeout=(8, 20), allow_redirects=False,
                                   headers={"User-Agent": "ZhorikStoreResearch/1.0 (+read-only; public catalog)"},
                                   stream=True)
            if response.status_code in (301, 302, 303, 307, 308):
                from urllib.parse import urljoin
                current = urljoin(current, response.headers.get("Location", ""))
                validate_url(current, store)
                response.close()
                continue
            response.raise_for_status()
            if "text/html" not in response.headers.get("Content-Type", ""):
                raise ValueError("Ответ не HTML")
            data = bytearray()
            for chunk in response.iter_content(32768):
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise ValueError("HTML больше 2 МБ")
            response.close()
            return store, extract_offers(data.decode("utf-8-sig", errors="replace"), store, current)
    raise ValueError("Слишком много перенаправлений")


# Conservative public sitemap discovery. No crawling search APIs or bypassing 403.
DISCOVERY_MAX = 200
SITEMAP_MAX = 12
SITEMAP_BYTES = 2_000_000


def _fetch_public_text(url, store="magnit", max_bytes=SITEMAP_BYTES):
    """Read bounded public sitemap/robots content with per-hop URL validation."""
    validate_url(url, store)
    with requests.Session() as session:
        current = url
        for _ in range(4):
            response = session.get(current, timeout=(8, 20), allow_redirects=False,
                                   headers={"User-Agent": "ZhorikStoreResearch/1.0 (+read-only; public sitemap)"},
                                   stream=True)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(current, response.headers.get("Location", ""))
                    validate_url(target, store)
                    current = target
                    continue
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_content(32768):
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Файл источника больше допустимого размера")
                return data.decode("utf-8-sig", errors="replace")
            finally:
                response.close()
    raise ValueError("Слишком много перенаправлений")


def _sitemap_locations(xml_text):
    root = ET.fromstring(xml_text)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in ("sitemapindex", "urlset"):
        raise ValueError("Это не XML sitemap")
    urls = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "loc" and el.text:
            urls.append(el.text.strip())
    return kind, urls


def discover_magnit(limit=100):
    """Find public product URLs from robots.txt and sitemap XML only.

    Discovery is best-effort: sites may publish no product links. Never claim
    that the resulting set represents the full catalog.
    """
    limit = max(1, min(DISCOVERY_MAX, int(limit)))
    seeds = ["https://magnit.ru/sitemap.xml"]
    notes = []
    try:
        robots = _fetch_public_text("https://magnit.ru/robots.txt", max_bytes=256_000)
        for line in robots.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                candidate = line.split(":", 1)[1].strip()
                try:
                    validate_url(candidate, "magnit")
                    if candidate not in seeds:
                        seeds.append(candidate)
                except ValueError:
                    pass
    except Exception as exc:
        notes.append(f"robots.txt: {type(exc).__name__}: {str(exc)[:100]}")
    queue = seeds[:SITEMAP_MAX]
    visited = set()
    products = []
    seen = set()
    while queue and len(visited) < SITEMAP_MAX and len(products) < limit:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            kind, locations = _sitemap_locations(_fetch_public_text(url))
        except Exception as exc:
            notes.append(f"{url}: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        for location in locations:
            try:
                validate_url(location, "magnit")
            except ValueError:
                continue
            if kind == "sitemapindex":
                if location not in visited and location not in queue and len(queue) < SITEMAP_MAX:
                    queue.append(location)
            elif urlparse(location).path.startswith("/product/") and location not in seen:
                seen.add(location)
                products.append(location)
                if len(products) >= limit:
                    break
    return products, visited, notes


@router.message(Command("magnit_discover"))
async def magnit_discover_command(message: Message):
    if not is_owner(message):
        return
    if message.chat.id in _running:
        await message.answer("Проверка уже выполняется")
        return
    _, _, arg = (message.text or "").partition(" ")
    try:
        limit = int(arg.strip()) if arg.strip() else 100
        if not 1 <= limit <= DISCOVERY_MAX:
            raise ValueError
    except ValueError:
        await message.answer("Формат: /magnit_discover 100 (от 1 до 200)")
        return
    _running.add(message.chat.id)
    try:
        products, visited, notes = await asyncio.to_thread(discover_magnit, limit)
        ROOT.mkdir(parents=True, exist_ok=True)
        report = ("МАГНИТ — ПОИСК ССЫЛОК В ПУБЛИЧНЫХ SITEMAP\n"
                  f"Запрошено: {limit}\nПроверено sitemap: {len(visited)}\n"
                  f"Найдено уникальных карточек: {len(products)}\n"
                  "Это не весь ассортимент. Цены не собирались и не менялись.\n"
                  + ("\nДиагностика:\n" + "\n".join(notes[:15]) if notes else ""))
        (ROOT / "last_discovery_report.txt").write_text(report, encoding="utf-8")
        (ROOT / "last_discovered_urls.txt").write_text("\n".join(products) + ("\n" if products else ""), encoding="utf-8")
        await message.answer_document(BufferedInputFile(report.encode("utf-8"), filename="magnit_discovery_report.txt"))
        if products:
            await message.answer_document(BufferedInputFile(("\n".join(products) + "\n").encode("utf-8"),
                                                            filename="magnit_discovered_urls.txt"),
                                          caption="Найденные карточки. Для проверки цен: /store_batch и TXT до 50 ссылок.")
    except Exception as exc:
        log.exception("Magnit sitemap discovery failed")
        await message.answer(f"Ошибка поиска sitemap: {type(exc).__name__}: {str(exc)[:150]}")
    finally:
        _running.discard(message.chat.id)


def parse_urls(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("Файл больше 64 КБ")
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("Нужен UTF-8 TXT") from exc
    urls = []
    for line in lines:
        line = re.sub(r"^\s*\d+\s*[.)]\s*", "", line).strip()
        if not line or line.startswith("#"):
            continue
        validate_url(line)
        urls.append(line)
    if not 1 <= len(urls) <= MAX_URLS:
        raise ValueError("Нужно 1–50 ссылок")
    return urls


def csv_bytes(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["source", "url", "fetched_at", "region", "raw_title", "price", "qty", "unit", "confidence"])
    writer.writeheader()
    for row in rows:
        safe = dict(row)
        for field in ("raw_title", "url"):
            value = str(safe.get(field) or "")
            if value.lstrip()[:1] in ("=", "+", "-", "@"):
                safe[field] = "'" + value
        writer.writerow(safe)
    return out.getvalue().encode("utf-8-sig")


async def run_batch(message, urls):
    chat_id = message.chat.id
    if chat_id in _running:
        await message.answer("Проверка уже выполняется")
        return
    _running.add(chat_id)
    records = []
    errors = []
    try:
        for i, url in enumerate(urls, 1):
            try:
                store, offers = await asyncio.to_thread(fetch_url, url)
                records.extend(offers)
                errors.append(f"{i}. {store}: {len(offers)} структурированных предложений — {url}")
            except Exception as exc:
                errors.append(f"{i}. ERROR {type(exc).__name__}: {str(exc)[:180]} — {url}")
                log.warning("Store lab request failed: %s", url, exc_info=True)
            if i < len(urls):
                await asyncio.sleep(1.5)
        ROOT.mkdir(parents=True, exist_ok=True)
        report = ("ЛАБОРАТОРИЯ МАГНИТ / ПЯТЁРОЧКА\n"
                  "Только публичные HTML-страницы и структурированные Product/Offer.\n"
                  "Пустой результат НЕ означает отсутствие товаров.\n"
                  "Регион, фасовка и наличие не подтверждены. Рабочие цены не изменены.\n\n"
                  + "\n".join(errors) + f"\n\nВсего предложений: {len(records)}")
        (ROOT / "last_report.txt").write_text(report, encoding="utf-8")
        (ROOT / "last_offers.csv").write_bytes(csv_bytes(records))
        await message.answer_document(BufferedInputFile(report.encode("utf-8"), filename="store_lab_report.txt"))
        await message.answer_document(BufferedInputFile(csv_bytes(records), filename="store_raw_offers.csv"),
                                      caption="Сырые предложения. НЕ импортировать напрямую в рабочие цены.")
    except Exception as exc:
        log.exception("Store lab failure")
        await message.answer(f"Лаборатория завершилась ошибкой: {type(exc).__name__}: {str(exc)[:150]}")
    finally:
        _running.discard(chat_id)


@router.message(Command("store_lab"))
async def menu(message: Message):
    if not is_owner(message):
        return
    _awaiting.discard(message.chat.id)
    await message.answer("🧪 Магнит + Пятёрочка (эксперимент)\n"
                         "Отправь /magnit_prices HTTPS-ссылка или /pyaterochka_prices HTTPS-ссылка.\n"
                         "Для массовой проверки: /store_batch, затем TXT (1 официальная ссылка в строке, до 50).\n"
                         "Для поиска карточек в публичных sitemap: /magnit_discover 100 (до 200).\nВернутся отчёт TXT и выгрузка CSV. Поиск по названию пока недоступен.\n"
                         "Свои проверенные цены загружай через существующее меню «Мои цены и импорт». ")


@router.message(Command("magnit_prices", "pyaterochka_prices"))
async def one_url(message: Message):
    if not is_owner(message):
        return
    command, _, url = (message.text or "").partition(" ")
    store = "magnit" if command.split("@")[0] == "/magnit_prices" else "pyaterochka"
    try:
        validate_url(url.strip(), store)
    except ValueError:
        await message.answer(f"Формат: {command.split('@')[0]} https://{'magnit.ru/catalog' if store == 'magnit' else '5ka.ru/catalog/'}\n"
                             "Поиск по названию товара пока недоступен; нужна ссылка на официальную страницу.")
        return
    await run_batch(message, [url.strip()])


@router.message(Command("store_batch"))
async def batch_menu(message: Message):
    if not is_owner(message):
        return
    _awaiting.add(message.chat.id)
    await message.answer("Пришли TXT-файл: по одной HTTPS-ссылке magnit.ru или 5ka.ru в строке, до 50. /store_lab — отмена.")


class AwaitingStoreDocument(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return is_owner(message) and message.chat.id in _awaiting


@router.message(F.document, AwaitingStoreDocument())
async def batch_document(message: Message):
    if not is_owner(message) or message.chat.id not in _awaiting:
        return
    _awaiting.discard(message.chat.id)
    name = (message.document.file_name or "").lower()
    size = message.document.file_size or 0
    if not name.endswith(".txt") or size > MAX_BYTES:
        await message.answer("Нужен TXT до 64 КБ")
        return
    try:
        buf = io.BytesIO()
        await message.bot.download(message.document, destination=buf)
        urls = parse_urls(buf.getvalue())
    except Exception as exc:
        await message.answer(f"Ошибка TXT: {exc}")
        return
    await run_batch(message, urls)


# Отдельные экспериментальные адаптеры; действующие команды сохранены.
from price_sources_lab import router as price_sources_lab_router
router.include_router(price_sources_lab_router)

# Единая owner-only лаборатория текстовых поисковых запросов.
from retail_batch_lab import router as retail_batch_lab_router
router.include_router(retail_batch_lab_router)
