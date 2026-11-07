"""One public Dixy sitemap and one product page; diagnostics only."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from retail_probe_http import ProbeError, fetch, shape


def diagnose():
    raw, _ = fetch("https://dixy.ru/sitemap-iblock-1.xml")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ProbeError("Sitemap не XML") from exc
    urls = [x.text.strip() for x in root.iter()
            if x.tag.endswith("}loc") and x.text and "/product/" in x.text]
    if not urls:
        raise ProbeError("В sitemap нет ссылок на товары")
    link = urls[0]
    parsed = urlparse(link)
    if parsed.scheme != "https" or parsed.hostname != "dixy.ru" or parsed.port not in (None, 443):
        raise ProbeError("Sitemap содержит недопустимый адрес")
    raw, _ = fetch(link)
    html = raw.decode("utf-8", errors="replace")
    match = re.search(r"module\.initElementData\(\s*", html)
    if not match:
        return ["Sitemap: ссылка на карточку найдена", "Карточка: initElementData отсутствует"]
    try:
        product, _ = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        return ["Sitemap: ссылка на карточку найдена", "Карточка: initElementData не разобран"]
    return ["Sitemap: ссылка на карточку найдена", "Карточка: " + shape(product)[:180]]
