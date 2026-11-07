"""Read-only Lenta Integration API probe for Moscow; no browser, auth or retries.

The Moscow center is a fixed *test coordinate*, not the user's address.
Store IDs and prices depend on actual API replies; none are invented.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import threading
import time
from urllib.parse import urljoin

import requests

BASE = "https://integration.api.lenta.com"
LAT, LON = "55.7558", "37.6173"
MAX_BYTES = 1_000_000
TIMEOUT = (6, 18)
_lock = threading.Lock()
_cached_store: tuple[int, float] | None = None


class LentaProbeError(Exception):
    pass


def _get(path: str, params: dict) -> object:
    """Only a fixed first-party API host; redirects and large bodies are rejected."""
    if path not in ("/v1/stores/nearest/hub", "/catalog/v1/items"):
        raise LentaProbeError("Недопустимый путь API")
    try:
        with requests.get(urljoin(BASE, path), params=params, timeout=TIMEOUT,
                          allow_redirects=False, stream=True,
                          headers={"Accept": "application/json"}) as response:
            if response.status_code in (401, 403, 429):
                raise LentaProbeError(f"HTTP {response.status_code}: доступ ограничен; повторов нет")
            if response.status_code in (301, 302, 303, 307, 308):
                raise LentaProbeError(f"HTTP {response.status_code}: перенаправление; повторов нет")
            if response.status_code != 200:
                raise LentaProbeError(f"HTTP {response.status_code}: ошибка источника")
            if "json" not in response.headers.get("Content-Type", "").lower():
                raise LentaProbeError("Ответ не JSON: возможно, защитная страница")
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=32768):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise LentaProbeError("Ответ превышает 1 МБ")
                chunks.append(chunk)
            return json.loads(b"".join(chunks).decode("utf-8"))
    except requests.RequestException as exc:
        raise LentaProbeError("Ошибка соединения: " + type(exc).__name__) from exc
    except (UnicodeError, ValueError) as exc:
        raise LentaProbeError("Некорректный JSON источника") from exc


def _rows(payload: object, keys: tuple[str, ...]) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in keys:
            found = payload.get(key)
            if isinstance(found, list):
                return [x for x in found if isinstance(x, dict)]
            if isinstance(found, dict):
                result = _rows(found, keys)
                if result:
                    return result
    return []


def _shape(payload: object) -> str:
    """Bounded JSON *structure* only: never expose response values or tokens."""
    def names(mapping: dict) -> str:
        safe = []
        for key in list(mapping)[:12]:
            label = re.sub(r"[^A-Za-z0-9_]", "_", str(key))[:30]
            if label:
                safe.append(label)
        return ",".join(safe)

    if isinstance(payload, list):
        first = next((x for x in payload if isinstance(x, dict)), None)
        return f"list[{len(payload)}]" + (f" item_keys={names(first)}" if first else "")
    if not isinstance(payload, dict):
        return type(payload).__name__
    parts = ["keys=" + names(payload)]
    for key in ("data", "items", "products", "results", "content", "hubs"):
        child = payload.get(key)
        if isinstance(child, list):
            parts.append(f"{key}:" + _shape(child))
        elif isinstance(child, dict):
            parts.append(f"{key}.keys=" + names(child))
            for subkey, subvalue in list(child.items())[:5]:
                safe = re.sub(r"[^\w]", "_", str(subkey))[:25]
                if isinstance(subvalue, list):
                    parts.append(f"{key}.{safe}:list[{len(subvalue)}]")
                elif isinstance(subvalue, dict):
                    parts.append(f"{key}.{safe}:dict")
    return " ".join(parts)[:145]


def diagnose() -> list[str]:
    """Two independent channels, one search each; report structure only."""
    store_id = moscow_store()
    lines = [f"Выбранный store ID: {store_id} (московская тестовая точка)"]
    for channel in ("lo", "cc"):
        try:
            payload = _get("/catalog/v1/items", {"stores": store_id,
                            "channel": channel, "retailBrand": "lo",
                            "query": "молоко", "limit": 5, "offset": 0})
            if isinstance(payload, dict):
                total = payload.get("total")
                total_text = str(total) if isinstance(total, int) and not isinstance(total, bool) else "неизвестно"
                items = payload.get("items")
                count = len(items) if isinstance(items, list) else "нет списка"
                lines.append(f"channel={channel}: total={total_text}, items={count}; " + _shape(payload))
            else:
                lines.append(f"channel={channel}: JSON " + _shape(payload))
        except LentaProbeError as exc:
            lines.append(f"channel={channel}: ERROR: " + str(exc)[:140])
    return lines


def moscow_store() -> int:
    """One nearest-store lookup cached for a day within this bot process."""
    global _cached_store
    with _lock:
        if _cached_store and time.monotonic() - _cached_store[1] < 86400:
            return _cached_store[0]
        response = _get("/v1/stores/nearest/hub", {"latitude": LAT, "longitude": LON,
                                                  "retailBrand": "lo"})
        hubs = _rows(response, ("hubs", "items", "data"))
        for hub in hubs:
            city = str(hub.get("city") or "").lower()
            ident = hub.get("id")
            if ("моск" in city or "moscow" in city) and str(ident).isdigit() and 0 < int(ident) <= 1_000_000_000:
                _cached_store = (int(ident), time.monotonic())
                return int(ident)
        raise LentaProbeError("API не подтвердил ID магазина Москвы")


def _money(value: object) -> str:
    if isinstance(value, dict):
        # Price objects vary by surface; require explicit monetary keys.
        for key in ("value", "amount", "price"):
            if key in value:
                return _money(value[key])
        return ""
    if value is None or isinstance(value, bool):
        return ""
    try:
        d = Decimal(str(value).replace(",", "."))
        return str(d) if d.is_finite() and d > 0 else ""
    except (InvalidOperation, TypeError, ValueError):
        return ""


def search(query: str) -> list[dict]:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 140:
        raise ValueError("Неверная поисковая фраза")
    store_id = moscow_store()
    payload = _get("/catalog/v1/items", {"stores": store_id, "channel": "lo",
                                         "retailBrand": "lo", "query": query.strip(),
                                         "limit": 10, "offset": 0})
    items = _rows(payload, ("items", "products", "results", "content", "data"))
    if not items:
        raise LentaProbeError("Каталог: 0 распознанных товаров; JSON " + _shape(payload))
    result = []
    for raw in items[:10]:
        product = raw.get("item") if isinstance(raw.get("item"), dict) else raw
        name = product.get("name") or product.get("title")
        ident = product.get("id") or product.get("itemId") or product.get("sku")
        if not isinstance(name, str) or not name.strip():
            continue
        price = (_money(product.get("price")) or _money(product.get("promoPrice")) or
                 _money(product.get("currentPrice")))
        result.append({"store": "lenta", "product_id": str(ident or ""),
                       "title": name[:300], "price_rub": price,
                       "store_id": str(store_id), "region": "Москва — ближайший хаб к тестовой точке",
                       "source_url": BASE + "/catalog/v1/items", "observed_at": datetime.now(timezone.utc).isoformat(),
                       "price_type": "API price (проверить скидки/канал)", "unit": "",
                       "review_status": "review_required"})
    if not result:
        raise LentaProbeError("Каталог: товары есть, но поля названия неизвестны; JSON " + _shape(items))
    return result
