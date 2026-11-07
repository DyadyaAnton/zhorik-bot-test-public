"""Read-only price intelligence for laboratory CSV files.

Never promotes a search candidate into an approved price. Aggregation requires
an explicit manually_confirmed review_status supplied by a reviewer.
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from statistics import median

from product_normalizer import normalize, match_product


def _decimal(value):
    try:
        result = Decimal(str(value).replace(",", "."))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def analyze_csv(data: bytes, *, max_rows: int = 50000) -> dict:
    """Analyze uploaded legacy batch CSV, without writing to any database."""
    if len(data) > 15_000_000:
        raise ValueError("CSV превышает лимит 15 МБ")
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Ожидается CSV в UTF-8") from exc
    reader = csv.DictReader(io.StringIO(source))
    if not reader.fieldnames or not {"store", "query", "title", "price_rub"}.issubset(reader.fieldnames):
        raise ValueError("CSV не содержит обязательные поля лаборатории")
    stats = Counter()
    examples: list[dict] = []
    confirmed: dict[tuple[str, str, str], list[Decimal]] = defaultdict(list)
    observed: set[tuple[str, str, str, str]] = set()
    for count, row in enumerate(reader, 1):
        if count > max_rows:
            raise ValueError(f"CSV содержит более {max_rows} строк")
        store, query = (row.get("store") or "").strip(), (row.get("query") or "").strip()
        stats["total_rows"] += 1
        stats["store_" + store[:30]] += 1
        if (row.get("status") or "") != "OK" or not row.get("title"):
            stats["empty_or_failed"] += 1
            continue
        normalized = normalize(row)
        match = match_product(normalized, query, "raw_ingredient")
        stats["match_" + match.status] += 1
        if normalized.unit_price_rub:
            stats["unit_price_calculated"] += 1
        else:
            stats["unit_price_unavailable"] += 1
        for warning in normalized.warnings:
            stats["warning_" + warning] += 1
        # The source's review status and the actual match are two independent
        # gates. A likely_match is never sufficient for approval on its own.
        canonical = (row.get("canonical_product_id") or "").strip()
        region = (row.get("region") or "").strip()
        if ((row.get("review_status") or "") == "manually_confirmed" and
                match.status == "likely_match" and canonical and region and region.lower() != "неизвестен"):
            price = _decimal(normalized.unit_price_rub)
            if price and normalized.unit_price_basis:
                identity = (store, (row.get("product_id") or row.get("title") or "").strip(),
                            (row.get("observed_at") or "").strip(), canonical)
                if identity not in observed:
                    observed.add(identity)
                    confirmed[(canonical, region, normalized.unit_price_basis)].append(price)
                    stats["confirmed_used_for_averages"] += 1
                else:
                    stats["duplicate_observations"] += 1
        elif (row.get("review_status") or "") == "manually_confirmed":
            stats["confirmed_but_not_aggregatable"] += 1
        if len(examples) < 35 and (match.status != "likely_match" or not normalized.unit_price_rub):
            examples.append({"store": store, "query": query, "title": (row.get("title") or "")[:130],
                             "match": match.status, "unit_price": normalized.unit_price_rub,
                             "basis": normalized.unit_price_basis, "warnings": list(normalized.warnings)})
    averages = []
    for (canonical, region, basis), numbers in sorted(confirmed.items()):
        if len(numbers) < 3:
            stats["insufficient_confirmed_groups"] += 1
            continue
        ordered = sorted(numbers)
        # Conservative nearest-rank quartiles, no interpolation.
        def quantile(frac):
            index = max(0, int((len(ordered) - 1) * frac))
            return str(ordered[index].quantize(Decimal("0.01")))
        averages.append({"canonical_product_id": canonical, "region": region, "basis": basis, "count": len(numbers),
                         "economy": quantile(.25),
                         "middle": str(Decimal(str(median(numbers))).quantize(Decimal("0.01"))),
                         "premium": quantile(.75), "status": "reviewed_sample_only"})
    return {"stats": dict(sorted(stats.items())), "candidate_examples": examples,
            "approved_price_groups": averages,
            "notice": "Кандидаты не записаны в рабочую БД. Для агрегирования нужны вручную подтверждённый canonical_product_id, регион и сопоставимые единицы; выборка не обязательно репрезентативна."}


def report_json(data: bytes) -> bytes:
    return json.dumps(analyze_csv(data), ensure_ascii=False, indent=2).encode("utf-8")


def diagnostic_status(details: list[str]) -> str:
    """Conservative summary; a single empty query must not label a source empty."""
    if any("SKIPPED" in line for line in details):
        return "SKIPPED"
    if any("SOFT_TIMEOUT" in line for line in details):
        return "PARTIAL_TIMEOUT"
    if any("ERROR" in line or "HTTP 403" in line or "HTTP 500" in line for line in details):
        return "ERROR_OR_PARTIAL"
    searches = [line for line in details if "товаров=" in line]
    if searches and all("товаров=0" in line for line in searches):
        return "EMPTY"
    if searches and any("товаров=0" in line for line in searches):
        return "PARTIAL"
    return "DIAGNOSTIC_OK_UNVERIFIED"
