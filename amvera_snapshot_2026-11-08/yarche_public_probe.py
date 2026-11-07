"""Minimal one-shot guest GraphQL search for Yarche; no user credentials."""
from __future__ import annotations

from retail_probe_http import ProbeError, fetch, json_body, shape


def search_products(query, limit=5):
    """Guest endpoint only; bounded query, never assert store or region."""
    from datetime import datetime, timezone
    from price_sources_core import money
    text = str(query).strip()
    if not text or len(text) > 80 or any(ord(ch) < 32 for ch in text):
        raise ValueError('Invalid Yarche search term')
    if type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError('Invalid result limit')
    _, headers = fetch('https://api.yarcheplus.ru/api/auth/token',
                       params={'shop_code': 'yarche'})
    token = next((value for key, value in headers.items() if key.lower() == 'token'), None)
    if not token:
        raise ProbeError('Guest token not available')
    gql = ('query($s:String!,$p:PageInput){search(search:$s,page:$p)'
           '{products{id name price} page{total limit page}}}')
    raw, _ = fetch('https://api.yarcheplus.ru/api/graphql', method='POST',
                   headers={'token': token, 'Accept': 'application/json'},
                   payload={'query': gql, 'variables': {'s': text,
                                                       'p': {'page': 1, 'limit': limit}}})
    data = json_body(raw)
    if not isinstance(data, dict) or data.get('errors'):
        raise ProbeError('Unexpected Yarche response')
    search = (data.get('data') or {}).get('search') or {}
    products = search.get('products') or []
    if not isinstance(products, list):
        raise ProbeError('Unexpected Yarche product list')
    observed = datetime.now(timezone.utc).isoformat()
    result = []
    for item in products[:limit]:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get('id') or '').strip()
        title = str(item.get('name') or '').strip()
        price = money(item.get('price'))
        if product_id and title and price:
            result.append({'product_id': product_id[:200], 'title': title[:180],
                           'price_rub': price, 'observed_at': observed,
                           'region': 'не подтверждён'})
    return result


def diagnose():
    _, headers = fetch("https://api.yarcheplus.ru/api/auth/token",
                       params={"shop_code": "yarche"})
    token = next((value for key, value in headers.items()
                  if key.lower() == "token"), None)
    if not token:
        raise ProbeError("Гостевой токен отсутствует в заголовках")
    gql = ('query($s:String!,$p:PageInput){search(search:$s,page:$p)'
           '{products{id name price} page{total limit page}}}')
    raw, _ = fetch("https://api.yarcheplus.ru/api/graphql", method="POST",
                   headers={"token": token, "Accept": "application/json"},
                   payload={"query": gql, "variables": {"s": "молоко",
                                                       "p": {"page": 1, "limit": 5}}})
    result = json_body(raw)
    if not isinstance(result, dict):
        raise ProbeError("GraphQL: неожиданный JSON")
    if result.get("errors"):
        raise ProbeError("GraphQL errors: " + str(len(result["errors"])))
    search = (result.get("data") or {}).get("search") or {}
    products = search.get("products") or []
    page = search.get("page") or {}
    return [f"Поиск молоко: products={len(products)}, total={page.get('total', 'нет')}",
            "Поля первого товара: " + (shape(products[0])[:180] if products else "товаров нет")]
