"""Read-only public Kuper catalog diagnostics. No cookies, impersonation or retries.

Routes were researched from the independent LGPLv3 project kuper_api;
its implementation is not copied or imported here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json

import requests


BASE = "https://kuper.ru"
MAX_BYTES = 1_000_000


class KuperProbeError(RuntimeError):
    pass


def _get(path: str, params: dict) -> dict:
    with requests.get(BASE + path, params=params, timeout=(5, 15),
                      allow_redirects=False, stream=True,
                      headers={"Accept": "application/json"}) as response:
        code = response.status_code
        if code in (301, 302, 303, 307, 308, 401, 403, 429):
            raise KuperProbeError(f"HTTP {code}: доступ ограничен или перенаправление; повторов нет")
        if code != 200:
            raise KuperProbeError(f"HTTP {code}: запрос не принят")
        if "json" not in response.headers.get("Content-Type", "").lower():
            raise KuperProbeError("Ответ не JSON; возможна защитная страница")
        data = bytearray()
        for part in response.iter_content(16_384):
            data.extend(part)
            if len(data) > MAX_BYTES:
                raise KuperProbeError("Ответ превышает лимит 1 МБ")
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise KuperProbeError("Некорректный JSON") from exc
    if not isinstance(result, dict):
        raise KuperProbeError("Ответ JSON не является объектом")
    return result


def discover_moscow_stores() -> list[dict]:
    """One lookup at a fixed central Moscow point; no user coordinates."""
    payload = _get("/api/v2/stores", {"lat": 55.7558, "lon": 37.6173,
                                      "shipping_method": "delivery", "page": 1, "per_page": 24})
    stores = payload.get("stores")
    if not isinstance(stores, list):
        raise KuperProbeError("Нет массива stores в ответе")
    result = []
    for store in stores[:24]:
        if not isinstance(store, dict):
            continue
        retailer = store.get("retailer") or {}
        if not isinstance(retailer, dict):
            retailer = {}
        result.append({"store_id": str(store.get("id", ""))[:32],
                       "name": str(store.get("name", ""))[:120],
                       "retailer": str(retailer.get("name") or retailer.get("slug") or "")[:100]})
    return result


def _money(value) -> str:
    try:
        amount = Decimal(str(value))
        return str(amount.quantize(Decimal("0.01"))) if amount.is_finite() and 0 < amount < 1_000_000 else ""
    except (InvalidOperation, ValueError, TypeError):
        return ""


def search(store_id: int, query: str) -> list[dict]:
    if not isinstance(store_id, int) or not 1 <= store_id <= 1_000_000_000:
        raise ValueError("Неверный числовой Store ID Купера")
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 100 or any(ord(c) < 32 for c in query):
        raise ValueError("Неверная поисковая фраза")
    payload = _get("/api/v2/products", {"sid": store_id, "q": query.strip(),
                                        "page": 1, "per_page": 5, "sort": "popularity"})
    products = payload.get("products")
    if not isinstance(products, list):
        raise KuperProbeError("Нет массива products в ответе")
    results = []
    for raw in products[:5]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("name") or "").strip()[:300]
        price = _money(raw.get("price"))
        if not title or not price:
            continue
        results.append({"store": "kuper", "product_id": str(raw.get("id") or ""),
                        "title": title, "price_rub": price,
                        "old_price_rub": _money(raw.get("original_price")),
                        "quantity": "", "unit": "", "unit_price_rub": "",
                        "store_id": str(store_id), "region": "не подтверждён",
                        "source_url": BASE + "/api/v2/products",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "status": "review_required",
                        "human_volume": str(raw.get("human_volume") or "")[:80],
                        "price_type": str(raw.get("price_type") or "")[:60],
                        "retailer_id": str(raw.get("retailer_id") or "")[:40]})
    return results
