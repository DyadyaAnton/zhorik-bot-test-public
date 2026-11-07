"""Single read-only HTML diagnostic of a public Auchan category.

No authenticated session, cookies, browser, redirects, retries or writes.
This does not attempt to bypass any retailer access restriction.
"""

from __future__ import annotations

from html.parser import HTMLParser

import requests


URL = "https://www.auchan.ru/catalog/zamorozhennye-produkty/"
MAX_BYTES = 1_000_000


class _Signals(HTMLParser):
    def __init__(self):
        super().__init__()
        self.jsonld = 0
        self.product_links = 0
        self.product_links_rel = 0
        self.product_links_abs = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("type", "").lower() == "application/ld+json":
            self.jsonld += 1
        if tag == "a":
            href = str(a.get("href") or "")
            if href.startswith("/product/"):
                self.product_links_rel += 1
                self.product_links += 1
            elif href.startswith(("https://www.auchan.ru/product/", "http://www.auchan.ru/product/", "https://auchan.ru/product/", "http://auchan.ru/product/")):
                self.product_links_abs += 1
                self.product_links += 1


def auchan_html_diagnostic() -> dict[str, object]:
    try:
        with requests.get(
            URL,
            headers={"Accept": "text/html", "User-Agent": "ZhorikPriceLab/1.0"},
            timeout=(5, 15), allow_redirects=False, stream=True,
        ) as response:
            status = response.status_code
            result: dict[str, object] = {"http_status": status}
            if status in (301, 302, 303, 307, 308):
                result["note"] = "Перенаправление запрещено"
                return result
            if status in (401, 403, 429):
                result["note"] = "Доступ ограничен; повторов нет"
                return result
            if status != 200:
                result["note"] = "Страница недоступна"
                return result
            if "text/html" not in response.headers.get("Content-Type", "").lower():
                result["note"] = "Ответ не HTML"
                return result
            data = bytearray()
            for chunk in response.iter_content(chunk_size=16384):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    result["note"] = "HTML больше 1 МБ; чтение остановлено"
                    return result
            parser = _Signals()
            parser.feed(data.decode(response.encoding or "utf-8", errors="replace"))
            result.update({
                "html_bytes": len(data),
                "jsonld_blocks": parser.jsonld,
                "product_links": parser.product_links,
                "product_links_rel": parser.product_links_rel,
                "product_links_abs": parser.product_links_abs,
                "note": "Только наличие разметки; цены не извлекались и не сохранялись",
            })
            return result
    except requests.RequestException as exc:
        return {"http_status": "network_error", "note": type(exc).__name__}
