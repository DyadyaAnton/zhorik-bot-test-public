"""Read-only, single-request diagnostic for Perekrestok's catalog API.

The URL shape was observed in the open-source Open-Inflation/perekrestok_api
project. This diagnostic deliberately does not copy browser-session code,
collect cookies, solve challenges, or retry an access denial.
"""

from __future__ import annotations

import json
import re

import requests


API_URL = "https://www.perekrestok.ru/api/customer/1.4.1.0/catalog/search/all"
MAX_BYTES = 1_000_000


def perekrestok_api_diagnostic(query: str) -> dict[str, object]:
    query = query.strip()
    if not query or len(query) > 80 or re.search(r"[\x00-\x1f\x7f]", query):
        raise ValueError("Нужен поисковый запрос длиной от 1 до 80 символов")

    try:
        with requests.get(
            API_URL,
            params={"textQuery": query, "entityTypes[]": "product"},
            headers={"Accept": "application/json", "User-Agent": "ZhorikPriceLab/1.0"},
            timeout=(5, 15),
            allow_redirects=False,
            stream=True,
        ) as response:
            status = response.status_code
            result: dict[str, object] = {"http_status": status}
            if status in (301, 302, 303, 307, 308):
                result["note"] = "Перенаправление запрещено; дальнейших запросов нет"
                return result
            if status in (401, 403, 429):
                result["note"] = "Доступ ограничен; повторов и обхода нет"
                return result
            if status != 200:
                result["note"] = "API вернул ошибку; тело ответа не читаем"
                return result
            if "json" not in response.headers.get("Content-Type", "").lower():
                result["note"] = "Ответ не JSON; возможна защитная страница"
                return result

            chunks = bytearray()
            for part in response.iter_content(chunk_size=16384):
                chunks.extend(part)
                if len(chunks) > MAX_BYTES:
                    result["note"] = "JSON превышает лимит 1 МБ; остановлено"
                    return result
            try:
                payload = json.loads(chunks)
            except (ValueError, UnicodeError):
                result["note"] = "Некорректный JSON"
                return result
            result["json_type"] = type(payload).__name__
            if isinstance(payload, dict):
                result["top_level_keys"] = sorted(str(k) for k in payload)[:20]
                content = payload.get("content")
                if isinstance(content, dict):
                    result["content_keys"] = sorted(str(k) for k in content)[:20]
                    products = content.get("products")
                    if isinstance(products, list):
                        result["products_count"] = len(products)
            result["note"] = "Только форма ответа; цены и данные не сохранялись"
            return result
    except requests.RequestException as exc:
        return {"http_status": "network_error", "note": type(exc).__name__}
