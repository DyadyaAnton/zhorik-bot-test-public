"""Patch 06 Phase A: offline product understanding, without Telegram/network/DB.

Raw store observations are never mutated. This module intentionally does not
approve prices or products for automatic use in a shopping budget.
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

ProductKind = Literal["basic_ingredient", "semi_prepared", "prepared_meal", "unknown"]
MatchStatus = Literal["likely_match", "likely_unrelated", "ambiguous"]
Intent = Literal["raw_ingredient", "ready_meal", "any"]

WARNING_CODES = frozenset({
    "package_total_ambiguous", "multiple_packages_in_title", "no_unit_from_source",
    "product_kind_uncertain", "price_zero_or_negative", "brand_only",
    "missing_title", "invalid_price", "llm_used",
})
SIGNAL_CODES = frozenset({"percentage_in_title", "package_from_title", "unit_from_source"})
REVIEW_STATUSES = frozenset({"unreviewed", "review_required", "auto_rejected", "manually_confirmed"})

_UNIT = r"(?:килограммов?|килограмма|кг|граммов?|грамма|гр|г|литров?|литра|л|миллилитров?|миллилитра|мл|штук|штуки|шт)"
_NUMBER = r"\d+(?:[.,]\d+)?"
_COUNT_NOUN = r"(?:шт\.?|штук|пак\.?|пакет(?:ов|а|ы)?|пакетик(?:ов|а|и)?|бутыл(?:ок|ки|ка)?|упаков(?:ок|ки|ка)?|бан(?:ок|ки|ка)?|пач(?:ек|ки|ка)?)"
_MULT_LEFT = re.compile(rf"(?<!\d)(\d{{1,3}})\s*(?:{_COUNT_NOUN})?\s*(?:[xх×*]|по)\s*({_NUMBER})\s*({_UNIT})(?![а-яёa-z])", re.I)
_MULT_RIGHT = re.compile(rf"(?<!\d)({_NUMBER})\s*({_UNIT})\s*(?:[xх×*]|по)\s*(\d{{1,3}})(?:\s*{_COUNT_NOUN})?(?![а-яёa-z\d])", re.I)
_COUNT_PO = re.compile(rf"(?<!\d)(\d{{1,3}})\s*{_COUNT_NOUN}\s+по\s*({_NUMBER})\s*({_UNIT})(?![а-яёa-z])", re.I)
_SIMPLE = re.compile(rf"(?<![\w.,])({_NUMBER})\s*({_UNIT})(?![а-яёa-z])", re.I)
_PERCENT = re.compile(r"\d+(?:[.,]\d+)?\s*%")
_READY = re.compile(r"\b(?:плов|салат|суп|борщ|лазанья|пицца|шаурма|роллы|готов(?:ый|ая|ое|ые)|вар[её]но.копч[её]н\w*|копч[её]н\w*|гриль|отварн\w*|запеч[её]н\w*|пюре(?:\s+\S+){0,3}\s+с\b|в\s+\S+\s+соусе\b|с\s+соусом\b)\b", re.I)
_SEMI = re.compile(r"\b(?:пельмени|вареники|наггетсы|котлеты|фри|по-деревенски|полуфабрикат\w*|панировк\w*)\b", re.I)
_BASIC = re.compile(r"\b(?:молоко|молочн\w*|яйц\w*|яйцо|картофел\w*|картошк\w*|рис|гречк\w*|крупа|яблок\w*|яблоко|курин\w*|цыпл[её]нк\w*|грудк\w*|филе|фарш|говяж\w*|овощ\w*|сахар|масло|вод[аы]\b|мука|фасол\w*|горош\w*|кукуруз\w*|морков\w*|лук|соль|перец|хлеб|сыр|сметан\w*|кофе|чай|кефир|йогурт)\b", re.I)
_BRANDS = frozenset({"увелка", "экомилк", "немолоко", "добрый", "greenfield", "ahmad", "nescafe"})
_NAME_ALIASES = {
    "картошка": "картофель", "картошки": "картофель", "картофеля": "картофель",
    "яблоки": "яблоко", "яблока": "яблоко", "яйца": "яйцо", "яиц": "яйцо",
    "гречки": "гречка", "цыпленка": "курица",
}


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip().replace(" ", "").replace(",", "."))
        return d if d.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _unit(value: str) -> str | None:
    u = value.lower().strip(". ")
    if u in {"кг", "килограмм", "килограмма", "килограммов"}: return "кг"
    if u in {"г", "гр", "грамм", "грамма", "граммов"}: return "г"
    if u in {"л", "литр", "литра", "литров"}: return "л"
    if u in {"мл", "миллилитр", "миллилитра", "миллилитров"}: return "мл"
    if u in {"шт", "штук", "штуки"}: return "шт"
    return None


def _to_base(quantity: Decimal, unit: str) -> tuple[Decimal, str, Decimal, str]:
    if unit in ("г", "кг"):
        grams = quantity * (1000 if unit == "кг" else 1)
        return grams, "г", grams / 1000, "кг"
    if unit in ("мл", "л"):
        ml = quantity * (1000 if unit == "л" else 1)
        return ml, "мл", ml / 1000, "л"
    return quantity, "шт", quantity, "шт"


@dataclass(frozen=True)
class PackageInfo:
    package_count: int
    item_quantity: Decimal
    item_unit: str
    total_quantity: Decimal
    total_unit: str
    base_quantity: Decimal
    base_unit: str

    def to_dict(self) -> dict[str, Any]:
        return {"package_count": self.package_count, "item_quantity": str(self.item_quantity),
                "item_unit": self.item_unit, "total_quantity": str(self.total_quantity),
                "total_unit": self.total_unit, "base_quantity": str(self.base_quantity),
                "base_unit": self.base_unit}


def parse_package(text: str) -> PackageInfo | None:
    """Conservative parser: return None when several incompatible packs occur."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.replace("\xa0", " ")
    compounds = []
    for regex in (_MULT_LEFT, _MULT_RIGHT, _COUNT_PO):
        for match in regex.finditer(text):
            if regex is _MULT_RIGHT:
                qty, unit, count = match.group(1), match.group(2), match.group(3)
            else:
                count, qty, unit = match.group(1), match.group(2), match.group(3)
            compounds.append((match.span(), count, qty, unit))
    unique = {(span, count, qty, unit) for span, count, qty, unit in compounds}
    # A '5 packages of 80g' substring may match two patterns at different offsets;
    # only accept it if all candidates describe the same pack and overlap.
    if unique:
        parsed = {(count, qty, _unit(unit)) for _, count, qty, unit in unique}
        starts = [span for span, _, _, _ in unique]
        if len(parsed) != 1 or any(a[1] <= b[0] or b[1] <= a[0] for a in starts for b in starts if a != b):
            return None
        count, qty, unit = next(iter(parsed))
        if unit is None:
            return None
        item = _dec(qty)
        n = int(count)
        if item is None or item <= 0 or not 1 <= n <= 1000:
            return None
        # Reject other independent quantities, but not the matched compound.
        lo, hi = min(x[0][0] for x in unique), max(x[0][1] for x in unique)
        remainder = text[:lo] + " " + text[hi:]
        if _SIMPLE.search(remainder):
            return None
    else:
        simples = list(_SIMPLE.finditer(text))
        if len(simples) != 1:
            return None
        item, unit = _dec(simples[0].group(1)), _unit(simples[0].group(2))
        n = 1
        if item is None or item <= 0 or unit is None:
            return None
        # '10 шт' with a separate box count, or two packs without a precise
        # multiplier syntax, cannot be interpreted safely.
        before = text[:simples[0].start()]
        after = text[simples[0].end():]
        # 'по 500 мл 3 раза' has an unbound multiplier; do not guess a pack.
        if re.search(r"\bпо\s*$", before, re.I) or re.search(r"\b\d+\s+раз(?:а)?\b", after, re.I):
            return None
        if re.search(r"\b\d+\s*(?:пак\.?|пакет\w*|уп\.?|упаков\w*|пач\.?|пачек|пачки|бут\.?|бутыл\w*|короб\w*)\b", before, re.I):
            return None
    total, total_unit, base, base_unit = _to_base(item * n, unit)
    if base <= 0 or (base_unit in ("кг", "л") and base > 1000):
        return None
    return PackageInfo(n, item, unit, total, total_unit, base, base_unit)


def classify(title: str) -> ProductKind:
    """Unknown is preferable to guessing based on a brand or unknown dish."""
    if not isinstance(title, str) or not title.strip(): return "unknown"
    value = title.lower().replace("ё", "е")
    if _READY.search(value): return "prepared_meal"
    if _SEMI.search(value): return "semi_prepared"
    if _BASIC.search(value): return "basic_ingredient"
    return "unknown"


def _llm_enabled() -> bool:
    """Reserved extension point; Phase A never calls an LLM."""
    return False


@dataclass(frozen=True)
class NormalizedObservation:
    raw: dict[str, Any]
    product_kind: ProductKind
    package: PackageInfo | None
    price_rub: str | None
    unit_price_rub: str | None
    unit_price_basis: str | None
    review_status: str
    warnings: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"raw": copy.deepcopy(self.raw), "product_kind": self.product_kind,
                "package": self.package.to_dict() if self.package else None,
                "price_rub": self.price_rub, "unit_price_rub": self.unit_price_rub,
                "unit_price_basis": self.unit_price_basis,
                "review_status": self.review_status, "warnings": list(self.warnings),
                "signals": list(self.signals)}


def _primitive(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_primitive(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize(observation: Any) -> NormalizedObservation:
    """Interpret a store record without changing its source data or approving it."""
    raw = _primitive(observation)
    if not isinstance(raw, dict):
        raise TypeError("Observation must be a dict or dataclass instance")
    raw = copy.deepcopy(raw)
    title = str(raw.get("title") or raw.get("name") or "").strip()
    price = _dec(raw.get("price_rub", raw.get("price")))
    warnings: list[str] = []
    signals: list[str] = []
    if not title:
        warnings.append("missing_title")
    if title.lower() in _BRANDS:
        warnings.append("brand_only")
    if price is None:
        warnings.append("invalid_price")
    elif price <= 0:
        warnings.append("price_zero_or_negative")
    if _PERCENT.search(title):
        signals.append("percentage_in_title")
    kind = classify(title)
    if kind == "unknown":
        warnings.append("product_kind_uncertain")
    # For VkusVill names may contain weight not included in the title.
    source = title
    if not parse_package(source) and raw.get("weight"):
        source = str(raw["weight"])
    package = parse_package(source)
    if package:
        signals.append("package_from_title")
    else:
        if len(list(_SIMPLE.finditer(source))) > 1:
            warnings.append("multiple_packages_in_title")
        if _SIMPLE.search(source) or re.search(r"\d\s*[xх×*]\s*\d", source):
            warnings.append("package_total_ambiguous")
        source_unit = _unit(str(raw.get("sale_unit") or raw.get("unit") or ""))
        if source_unit in ("кг", "л"):
            total, total_u, base, base_u = _to_base(Decimal(1), source_unit)
            package = PackageInfo(1, Decimal(1), source_unit, total, total_u, base, base_u)
            signals.append("unit_from_source")
        else:
            warnings.append("no_unit_from_source")
    unit_price = None
    basis = None
    if package and price is not None and price > 0:
        basis = package.base_unit
        unit_price = str(price / package.base_quantity)
    rejected = not title or price is None or price <= 0
    return NormalizedObservation(raw, kind, package,
                                 str(price) if price is not None and price > 0 else None,
                                 unit_price, basis,
                                 "auto_rejected" if rejected else "review_required",
                                 tuple(dict.fromkeys(warnings)), tuple(dict.fromkeys(signals)))


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    query: str
    intent: Intent
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "query": self.query,
                "intent": self.intent, "reasons": list(self.reasons)}


def _tokens(s: str) -> set[str]:
    words = re.findall(r"[а-яёa-z]+", s.lower().replace("ё", "е"))
    return {_NAME_ALIASES.get(w, w) for w in words if len(w) >= 3}


def match_product(normalized: NormalizedObservation, query: str,
                  intent: Intent = "any") -> MatchResult:
    if intent not in ("raw_ingredient", "ready_meal", "any"):
        raise ValueError("Unknown shopping intent")
    name = str(normalized.raw.get("title") or normalized.raw.get("name") or "")
    q = query.strip().lower() if isinstance(query, str) else ""
    if not q or not name:
        return MatchResult("ambiguous", str(query), intent, ("missing_query_or_title",))
    q_kind = classify(q)
    if intent == "raw_ingredient" and normalized.product_kind in ("semi_prepared", "prepared_meal"):
        return MatchResult("likely_unrelated", q, intent, ("wrong_product_kind",))
    if intent == "ready_meal" and normalized.product_kind == "basic_ingredient":
        return MatchResult("likely_unrelated", q, intent, ("wrong_product_kind",))
    if q_kind in ("semi_prepared", "prepared_meal") and normalized.product_kind != q_kind:
        return MatchResult("likely_unrelated", q, intent, ("wrong_product_kind",))
    q_tokens, name_tokens = _tokens(q), _tokens(name)
    # Explicitly handle frequent shopper synonyms without pretending that all
    # morphological variants are known to the module.
    pairs = (("картофель", "картофел"), ("курица", "курин"),
             ("грудка", "грудк"), ("яблоко", "яблок"),
             ("яйцо", "яйц"), ("гречка", "гречнев"))
    def close(token: str, other: str) -> bool:
        return token == other or (len(token) >= 4 and len(other) >= 4 and
                                  (token.startswith(other[:4]) or other.startswith(token[:4]))) or any(
            (token.startswith(a[:4]) and other.startswith(b[:4])) or
            (token.startswith(b[:4]) and other.startswith(a[:4])) for a, b in pairs)
    matches = sum(any(close(word, candidate) for candidate in name_tokens) for word in q_tokens)
    if q_tokens and matches == len(q_tokens):
        return MatchResult("likely_match", q, intent)
    if matches == 0:
        return MatchResult("likely_unrelated", q, intent, ("no_shared_product_terms",))
    return MatchResult("ambiguous", q, intent, ("partial_term_match",))


__all__ = ["PackageInfo", "NormalizedObservation", "MatchResult", "WARNING_CODES",
           "SIGNAL_CODES", "REVIEW_STATUSES", "parse_package", "classify",
           "normalize", "match_product"]
