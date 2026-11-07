"""Conservative presentation and unit-price normalization for unverified MCP candidates.

No automatic link to nutrition_products or active price tables.
"""
import html
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

_UNIT = r'(?:кг|гр|г|мл|л)'
_PACK = re.compile(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*('+_UNIT+r')(?![а-яё])\.?', re.I)
_MULT = re.compile(r'(?<![\d.,])(\d+)\s*(?:шт\.?\s*)?[xх×*]\s*(\d+(?:[.,]\d+)?)\s*('+_UNIT+r')(?![а-яё])\.?', re.I)
_REVERSE_MULT = re.compile(r'(?<![\d.,])(\d+(?:[.,]\d+)?)\s*('+_UNIT+r')\.?\s*[xх×*]\s*(\d+)(?:\s*шт\.?)?(?!\d)', re.I)
_PER_COUNT = re.compile(r'(?<!\d)(\d+)\s*(?:шт\.?|штук|бутыл(?:ок|ки)|бан(?:ок|ки)|упаков(?:ок|ки)|пакет(?:ов|ика|иков)|пауч(?:ей|а))\s+по\s+(\d+(?:[.,]\d+)?)\s*('+_UNIT+r')(?![а-яё])\.?', re.I)
_COUNT_HINT = re.compile(r'\b(?:набор|упаковка|уп\.?|блок)\b[^)]{0,65}\b\d+\s*шт\b', re.I)
_MAX_BASE_QTY = Decimal('100')
_READY = re.compile(r'\b(котлет(?:а|ы|ами|ой)?|отварн(?:ая|ый|ое|ые)|готов(?:ая|ый|ое|ые)|суп|салат|гарнир|по-купечески|с\s+овощами|ассорти\s+каш|каша\s+с|плов|лазанья|голубц(?:ы|ов)|бифштекс\s+с|шаурма|роллы)\b', re.I)
_SEMI = re.compile(r'\b(пельмен(?:и|ей)|вареник(?:и|ов)|наггетс(?:ы|ов)|замороженн(?:ые|ый|ая)\s+котлеты)\b', re.I)
_PACKETS = re.compile(r'\b(пакетик(?:и|ах|ов)?|набор|ассорти)\b', re.I)
_WEIGHT_LABEL = re.compile(r'(?:^|[\s,(])ВЕС(?:$|[\s,).])', re.I)

def clean_name(name):
    return ' '.join(html.unescape(str(name or '')).replace('\xa0', ' ').split())


def classify(name):
    """Only a conservative flag; ambiguous items must be reviewed manually."""
    name = clean_name(name)
    if _SEMI.search(name):
        return 'semi_prepared'
    if _READY.search(name):
        return 'prepared_or_mixed'
    if _PACKETS.search(name):
        return 'review_required'
    return 'review_required'  # no automatic approval based on text alone


def normalize_candidate(row):
    """Fail closed on ambiguous packaging; only observed catalog data is used."""
    name = clean_name(row.get('name'))
    weight = clean_name(row.get('weight'))
    unit_field = clean_name(row.get('unit')).lower().strip(' .')
    out = {'clean_name': name, 'category_status': classify(name),
           'pack_amount': None, 'base_unit': None, 'price_per_base_unit': None,
           'unit_price_status': 'unknown'}
    try:
        price = Decimal(str(row.get('price_rub')))
        if not price.is_finite() or price <= 0:
            return out
    except (InvalidOperation, TypeError, ValueError):
        return out

    # MCP sale unit is authoritative. "ВЕС" is an additional catalog marker.
    if unit_field in ('кг', 'килограмм') or (not unit_field and _WEIGHT_LABEL.search(name)):
        out.update(pack_amount=1.0, base_unit='кг',
                   price_per_base_unit=float(price),
                   unit_price_status='estimated_public_catalog')
        return out
    # Other sale units are not assumed to be kg/l.
    name_for_pack = re.sub(r'(?<=[а-яё]),(?=\d)', ', ', name, flags=re.I)
    source = name_for_pack if _PACK.search(name_for_pack) else weight
    matches = list(_PACK.finditer(source))
    if len(matches) != 1:
        return out
    compounds = []
    for pattern in (_MULT, _REVERSE_MULT, _PER_COUNT):
        compounds.extend(pattern.finditer(source))
    if len(compounds) > 1:
        return out
    match = matches[0]
    if compounds:
        compound = compounds[0]
        if compound.re is _REVERSE_MULT:
            if compound.span(1) != match.span(1):
                return out
            multiplier_str = compound.group(3)
        else:
            if compound.span(2) != match.span(1):
                return out
            multiplier_str = compound.group(1)
    elif _COUNT_HINT.search(source):
        return out
    else:
        multiplier_str = '1'
        # "4 шт по 125 г" should be recognized; an unknown pack count must not be guessed.
        if re.search(r'\b\d+\s*(?:шт|штук|бутылок|банок|паучей)\b', source, re.I):
            return out
    try:
        qty = Decimal(match.group(1).replace(',', '.'))
        multiplier = Decimal(multiplier_str)
        if qty <= 0 or multiplier <= 0 or multiplier > 1000:
            return out
    except InvalidOperation:
        return out
    unit = match.group(2).lower()
    base = 'кг' if unit in ('кг', 'г', 'гр') else 'л'
    base_qty = qty * multiplier / (Decimal(1000) if unit in ('г', 'гр', 'мл') else Decimal(1))
    if base_qty <= 0 or base_qty > _MAX_BASE_QTY:
        return out
    out.update(pack_amount=float(base_qty), base_unit=base,
               price_per_base_unit=float((price / base_qty).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
               unit_price_status='estimated_public_catalog')
    return out
