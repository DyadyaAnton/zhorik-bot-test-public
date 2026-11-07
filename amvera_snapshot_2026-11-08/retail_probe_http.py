"""Bounded HTTP helpers for isolated read-only retail diagnostics."""
from __future__ import annotations

import json
from urllib.parse import urlparse

import requests

MAX_BYTES = 8_000_000  # Dixy product sitemap can be larger than a JSON reply.
TIMEOUT = (6, 18)
HOSTS = {"dixy.ru", "api.yarcheplus.ru"}


class ProbeError(Exception):
    pass


def fetch(url, *, params=None, method="GET", payload=None, headers=None):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.port not in (None, 443):
        raise ProbeError("Недопустимый адрес источника")
    try:
        with requests.request(method, url, params=params, json=payload, headers=headers,
                              timeout=TIMEOUT, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ProbeError(f"HTTP {response.status_code}")
            chunks, total = [], 0
            for part in response.iter_content(32768):
                total += len(part)
                if total > MAX_BYTES:
                    raise ProbeError("Ответ превышает 8 МБ")
                chunks.append(part)
            return b"".join(chunks), dict(response.headers)
    except requests.RequestException as exc:
        raise ProbeError("Ошибка соединения: " + type(exc).__name__) from exc


def json_body(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise ProbeError("Ответ не JSON") from exc


def shape(value, depth=2):
    """Structure only, bounded; never include scalar values or auth tokens."""
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        if depth <= 0:
            return "dict"
        parts = []
        for key, child in list(value.items())[:6]:
            label = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(key))[:22]
            parts.append(label + ":" + shape(child, depth - 1))
        return "{" + ",".join(parts) + "}"
    return type(value).__name__
