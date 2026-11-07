"""Read-only *logical* catalog across canonical, discovered and personal data.

Historical personal prices remain private in `user_prices` until migration
has been audited. This view never promotes an unknown retailer SKU to a
verified product. The caller is responsible for authorizing `user_id`.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation


def key(value):
    return ' '.join(str(value or '').strip().casefold().replace('ё', 'е').split())


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def catalog_snapshot(db, user_id, *, include_market=False, market_limit=5000):
    """Return one catalog view, not competing legacy price lists.

    `personal_offers` are scoped by the explicit user id; retail observations
    are global but carry their verification status and exact SKU identity.
    """
    if type(user_id) is not int or user_id <= 0:
        raise ValueError('Authenticated user ID required')
    if type(market_limit) is not int or not 1 <= market_limit <= 20000:
        raise ValueError('Invalid market export limit')
    entries = {}
    by_name = defaultdict(list)
    if _table(db, 'pb_products'):
        for code, name, category, unit in db.execute('SELECT code,name,category,base_unit FROM pb_products'):
            entries[code] = {'code': code, 'name': name, 'category': category,
                             'unit': unit, 'status': 'canonical',
                             'personal_offers': [], 'retail_offers': 0,
                             'potential_unlinked_observations': 0}
            by_name[key(name)].append(code)
    if _table(db, 'p7_discovered_products'):
        from price07_rev2_core import eligible_for_menu
        for code, name, kbju, allergens, review in db.execute('''
                SELECT code,canonical_name,kbju_verified,allergens_verified,review_status
                FROM p7_discovered_products'''):
            entries[code] = {'code': code, 'name': name, 'category': 'Новые товары',
                             'unit': None,
                             'status': 'verified' if kbju and allergens and review == 'verified'
                                       and eligible_for_menu(db, code)
                                       else 'review_required',
                             'personal_offers': [], 'retail_offers': 0,
                             'potential_unlinked_observations': 0}
            by_name[key(name)].append(code)
    personal_count = 0
    unmatched = 0
    if _table(db, 'user_prices'):
        for product, shop, unit, price, date in db.execute('''SELECT product,shop,unit,price,observed_at
            FROM user_prices WHERE user_id=? ORDER BY product,shop''', (user_id,)):
            personal_count += 1
            matches = [code for code in by_name.get(key(product), ())
                       if entries[code]['unit'] in (None, unit)]
            if len(matches) != 1:
                # Preserve arbitrary personal goods and ambiguities. Never
                # silently attach a different nutritional product.
                code = 'personal:' + key(product) + ':' + unit
                if code not in entries:
                    entries[code] = {'code': code, 'name': product,
                                     'category': 'Личные товары', 'unit': unit,
                                     'status': 'personal_unlinked',
                                     'personal_offers': [], 'retail_offers': 0,
                                     'potential_unlinked_observations': 0}
                    unmatched += 1
            else:
                code = matches[0]
            entries[code]['personal_offers'].append({
                'shop': shop, 'price': float(price), 'unit': unit, 'observed_at': date,
                'quality': 'personal', 'user_id': user_id})
    observed_count = 0
    confirmed_count = 0
    unlinked_skus = 0
    if _table(db, 'pb_skus') and _table(db, 'pb_observations'):
        for code, title, review, matches, count in db.execute('''
            SELECT s.product_code,s.title,o.review_status,s.match_status,COUNT(*)
            FROM pb_observations o JOIN pb_skus s ON s.id=o.sku_id
            GROUP BY s.id,o.review_status,s.match_status'''):
            observed_count += count
            if code in entries:
                entries[code]['retail_offers'] += count
            elif code is None:
                candidates = [candidate for candidate in by_name.get(key(title), ())
                              if entries[candidate]['status'] == 'review_required']
                if len(candidates) == 1:
                    # An identical title is only a *potential* discovery
                    # match, never an SKU-to-product binding.
                    entries[candidates[0]]['potential_unlinked_observations'] += count
            if code in entries and review in ('auto_verified','manually_verified') \
                    and matches in ('auto_verified','manually_verified'):
                confirmed_count += count
        unlinked_skus=db.execute('SELECT COUNT(*) FROM pb_skus WHERE product_code IS NULL').fetchone()[0]
    market_offers = []
    if include_market and _table(db, 'pb_skus') and _table(db, 'pb_observations'):
        for row in db.execute('''SELECT s.retailer,s.external_id,s.title,s.product_code,
            o.current_price_rub,o.unit_price_rub,o.base_unit,o.store_code,
            o.region,o.region_confirmed,o.review_status,o.observed_at,o.source
            FROM pb_observations o JOIN pb_skus s ON s.id=o.sku_id
            ORDER BY o.id DESC LIMIT ?''', (market_limit,)):
            (retailer, sku, title, code, price, unit_price, unit, store,
             region, region_confirmed, review, date, source) = row
            market_offers.append({'retailer': retailer, 'sku': sku, 'title': title,
                                  'product_code': code, 'price': price,
                                  'unit_price': unit_price, 'unit': unit,
                                  'store': store, 'region': region if region_confirmed else None,
                                  'status': review, 'date': date, 'source': source})
    return {'products': sorted(entries.values(), key=lambda p: (key(p['name']), p['code'])),
            'products_total': len(entries),
            'personal_offers_total': personal_count,
            'personal_unlinked_products': unmatched,
            'retail_unlinked_sku_groups': unlinked_skus,
            'retail_observations_total': observed_count,
            'retail_verified_linked_total': confirmed_count,
            'market_offers': market_offers,
            'market_truncated': include_market and observed_count > len(market_offers)}


def personal_prices_by_name(db, user_id):
    """Conservative per-user reference quotes, without artificial averages."""
    snap = catalog_snapshot(db, user_id)
    result = {}
    for product in snap['products']:
        for offer in product['personal_offers']:
            try:
                amount = Decimal(str(offer['price']))
            except (InvalidOperation, ValueError):
                continue
            if amount.is_finite() and amount > 0:
                result.setdefault((key(product['name']), offer['unit']), []).append(offer)
    return result


def search_catalog(db, user_id, query, *, limit=12, offset=0):
    """Search only the authenticated user's logical catalog, no other accounts."""
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
        raise ValueError('Enter 1-100 search characters')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Invalid search limit')
    if type(offset) is not int or not 0 <= offset <= 100000:
        raise ValueError('Invalid search offset')
    phrase = key(query)
    result = [item for item in catalog_snapshot(db, user_id)['products']
              if phrase in key(item['name'])]
    return result[offset:offset+limit], len(result)


def catalog_product_detail(db, user_id, code, *, limit=20):
    """Personal quotes and read-only SKU history for one visible catalog item.

    Observations are raw records, never automatically promoted to verified
    market prices; unlinked SKUs remain unlinked.
    """
    if not isinstance(code,str) or not 1 <= len(code) <= 150:
        raise ValueError('Invalid catalog code')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Invalid history limit')
    snapshot=catalog_snapshot(db,user_id)
    item=next((p for p in snapshot['products'] if p['code']==code),None)
    if item is None:
        raise KeyError('Catalog product not found')
    history=[]
    if _table(db,'pb_skus') and _table(db,'pb_observations'):
        for row in db.execute('''SELECT s.retailer,s.title,o.current_price_rub,
                    o.unit_price_rub,o.base_unit,o.store_code,o.region,
                    o.region_confirmed,o.review_status,s.match_status,o.observed_at
                 FROM pb_skus s JOIN pb_observations o ON o.sku_id=s.id
                 WHERE s.product_code=? ORDER BY o.observed_at DESC,o.id DESC LIMIT ?''',
                              (code,limit)):
            (retailer,title,price,unit_price,unit,store,region,region_ok,
             review,match,observed)=row
            verified=(review in ('auto_verified','manually_verified') and
                      match in ('auto_verified','manually_verified') and
                      bool(region_ok) and bool(region))
            history.append({'retailer':retailer,'title':title,'price':price,
                            'unit_price':unit_price,'unit':unit,'store':store,
                            'region':region if region_ok else None,
                            'quality':'verified_identity_region' if verified else 'review_required',
                            'observed_at':observed})
    return {'product':item,'history':history}


def export_catalog_txt(db, user_id):
    """Single user-facing report; legacy personal import remains separate."""
    snap = catalog_snapshot(db, user_id, include_market=True)
    lines = ['ЖОРИК · ЕДИНЫЙ КАТАЛОГ ТОВАРОВ И ЦЕН',
             f'Товарных позиций: {snap["products_total"]}',
             f'Магазинных наблюдений: {snap["retail_observations_total"]}',
             f'Личных предложений: {snap["personal_offers_total"]}',
             'Все непроверенные данные — ориентиры, не цены на кассе.', '']
    for item in snap['products']:
        lines.append(f'{item["name"]} [{item["code"]}] · {item["status"]}'
                     f' · магазинных наблюдений {item["retail_offers"]}')
        for offer in item['personal_offers']:
            lines.append(f'  💛 Личная цена: {offer["price"]:.2f} ₽/{offer["unit"]}'
                         f' · {offer["shop"]} · {offer["observed_at"]}')
    lines.extend(['', 'МАГАЗИННЫЕ НАБЛЮДЕНИЯ (без автоподтверждения)'])
    for offer in snap['market_offers']:
        lines.append(f'{offer["title"]} [{offer["retailer"]}/{offer["sku"]}] '
                     f'· {offer["price"]} ₽ за SKU; '
                     f'{offer["unit_price"]} ₽/{offer["unit"]} '
                     f'· магазин {offer["store"] or "неизвестен"}'
                     f' · регион {offer["region"] or "не подтверждён"}'
                     f' · {offer["status"]} · {offer["date"]}'
                     f' · источник {offer["source"]} '
                     f'· привязка {offer["product_code"] or "не проверена"}')
    if snap['market_truncated']:
        lines.append('⚠️ Вывод ограничен последними 5000 наблюдениями; '
                     'для полного архива используйте резервную копию SQLite.')
    return '\n'.join(lines)+'\n'
