# -*- coding: utf-8 -*-
"""Hard filters and ranking rules for Avito search-result cards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ListingEvaluation:
    rooms: int | None
    area: float | None
    is_short_term: bool
    hard_filter_passed: bool
    filter_reason: str | None
    room_check_pending: bool
    score: int
    score_reasons: dict[str, Any]
    preferred_area_match: bool
    negative_area_match: bool
    other_utilities: int | None
    effective_price: int | None


def _combined_text(*values: str | None) -> str:
    return " ".join(value for value in values if value)


def extract_rooms(*values: str | None) -> int | None:
    """Extract a room count from a title or a parameter string."""
    patterns = (
        r"(?<!\d)(\d+)\s*[-–—]?\s*к(?:\.|\b)",
        r"(?<!\d)(\d+)\s*(?:комнат(?:а|ы)?|комн\.?)\b",
        r"(?<!\d)(\d+)\s*спальн(?:я|и|ь)?\b",
        r"количество\s+комнат\s*:?\s*(\d+)",
    )
    for value in values:
        text = (value or "").lower().replace("ё", "е")
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return int(match.group(1))
        if "студия" in text:
            return 0
    return None


def extract_area(*values: str | None) -> float | None:
    """Extract the first dwelling area in square metres."""
    text = _combined_text(*values).lower().replace("\u00a0", " ")
    match = re.search(r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*м(?:²|2|\b)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", "."))


def is_short_term_rental(price_text: str | None, raw_text: str | None) -> bool:
    """Return True only when the card explicitly looks like a short stay."""
    price = (price_text or "").lower().replace("ё", "е")
    raw = (raw_text or "").lower().replace("ё", "е")
    combined = f"{price} {raw}"

    if "в месяц" in price or "/мес" in price:
        return False
    if re.search(r"не\s+(?:сда[её]тся\s+)?посуточ", combined):
        return False
    if "только на длительный срок" in combined:
        return False

    markers = (
        r"посуточ",
        r"за\s+\d+\s*сут",
        r"(?:₽|руб(?:\.|лей)?)\s*(?:/|за)\s*сут",
        r"за\s+ночь",
    )
    return any(re.search(pattern, combined) for pattern in markers)


def _normalize_address(value: str) -> str:
    value = value.lower().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", value).split())


def address_matches(address: str | None, preferred_patterns: Iterable[str]) -> bool:
    normalized_address = _normalize_address(address or "")
    if not normalized_address:
        return False
    return any(
        normalized_pattern and normalized_pattern in normalized_address
        for normalized_pattern in (_normalize_address(pattern) for pattern in preferred_patterns)
    )


def extract_other_utilities(parameters: Iterable[str]) -> int | None:
    for parameter in parameters:
        normalized = parameter.lower().replace("\u00a0", " ").replace("ё", "е")
        match = re.search(r"другие\s+жку\s*:?\s*([\d ]+)", normalized)
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            return int(digits) if digits else None
    return None


def evaluate_listing(
    *,
    category: str,
    title: str | None,
    price: int | None,
    price_text: str | None,
    address: str | None,
    params: str | None,
    raw_text: str | None,
    minimum_rooms: int = 2,
    exclude_short_term: bool = True,
    minimum_area: float = 54,
    maximum_price: int = 40_000,
    preferred_address_patterns: Iterable[str] = (),
    negative_address_patterns: Iterable[str] = (),
    detail_rooms: int | None = None,
    detail_area: float | None = None,
    detail_parameters: Iterable[str] = (),
    area_points: int = 1,
    price_points: int = 1,
    preferred_street_points: int = 2,
    neutral_street_points: int = 1,
    negative_street_points: int = -1,
    green_price_max: int = 30_000,
    red_price_max: int = 50_000,
    show_children: bool = True,
    show_commission: bool = True,
    show_utilities: bool = True,
) -> ListingEvaluation:
    search_rooms = extract_rooms(title, params, raw_text)
    rooms = detail_rooms if detail_rooms is not None else search_rooms
    search_area = extract_area(title, params)
    area = detail_area if detail_area is not None else search_area
    short_term = is_short_term_rental(price_text, raw_text)

    is_house = category == "houses_cottages"
    room_check_pending = is_house and rooms is None
    filter_reason: str | None = None
    if exclude_short_term and short_term:
        filter_reason = "short_term"
    elif rooms is not None and rooms < minimum_rooms:
        filter_reason = "not_enough_rooms"
    elif not is_house and rooms is None:
        filter_reason = "rooms_unknown"

    commission_text = _combined_text(params, raw_text).lower().replace("ё", "е")
    no_commission = "без комиссии" in commission_text or bool(
        re.search(r"комиссия\s*:?[ ]*0(?:\s*%|\b)", commission_text)
    )
    commission_present = "комисси" in commission_text and not no_commission
    preferred_match = address_matches(address, preferred_address_patterns)
    negative_match = address_matches(address, negative_address_patterns)
    other_utilities = extract_other_utilities(detail_parameters)
    effective_price = (
        price + (other_utilities or 0) if price is not None else None
    )
    score_reasons = {
        "area_54_plus": area is not None and area >= minimum_area,
        "no_commission": no_commission,
        "commission_present": commission_present,
        "price_40000_or_less": effective_price is not None and effective_price <= maximum_price,
        "preferred_area": preferred_match,
        "negative_area": negative_match,
        "other_utilities": other_utilities,
        "effective_price": effective_price,
        "area_points": area_points,
        "price_points": price_points,
        "green_price_max": green_price_max,
        "maximum_price": maximum_price,
        "red_price_max": red_price_max,
        "maximum_score": area_points + price_points + preferred_street_points,
        "show_children": show_children,
        "show_commission": show_commission,
        "show_utilities": show_utilities,
    }
    street_score = (
        negative_street_points
        if negative_match
        else preferred_street_points
        if preferred_match
        else neutral_street_points
    )
    score_reasons["street_score"] = street_score
    score = (
        int(score_reasons["area_54_plus"]) * area_points
        + int(score_reasons["price_40000_or_less"]) * price_points
        + street_score
    )

    return ListingEvaluation(
        rooms=rooms,
        area=area,
        is_short_term=short_term,
        hard_filter_passed=filter_reason is None,
        filter_reason=filter_reason,
        room_check_pending=room_check_pending,
        score=score,
        score_reasons=score_reasons,
        preferred_area_match=preferred_match,
        negative_area_match=negative_match,
        other_utilities=other_utilities,
        effective_price=effective_price,
    )
