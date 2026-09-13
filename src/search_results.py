# -*- coding: utf-8 -*-
"""Extraction of public listing cards from an Avito search result page."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag


AVITO_BASE_URL = "https://www.avito.ru"


@dataclass
class SearchListing:
    id: str
    category: str
    url: str
    title: Optional[str]
    price: Optional[int]
    price_text: Optional[str]
    address: Optional[str]
    params: Optional[str]
    published_text: Optional[str]
    description_snippet: Optional[str]
    raw_text: str

    def to_dict(self) -> dict:
        return asdict(self)


def _first_text(card: Tag, selectors: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        element = card.select_one(selector)
        if element:
            text = element.get_text(" ", strip=True)
            if text:
                return " ".join(text.split())
    return None


def _extract_id(card: Tag, url: str) -> Optional[str]:
    for attribute in ("data-item-id", "data-id"):
        value = card.get(attribute)
        if value:
            return str(value)

    match = re.search(r"_(\d+)(?:[/?#]|$)", url)
    return match.group(1) if match else None


def _extract_price(card: Tag, price_text: Optional[str]) -> Optional[int]:
    price_meta = card.select_one('[itemprop="price"]')
    if price_meta:
        content = price_meta.get("content")
        if content and str(content).isdigit():
            return int(content)

    if not price_text:
        return None

    match = re.search(r"\d[\d\s\u00a0]*", price_text)
    if not match:
        return None

    digits = re.sub(r"\D", "", match.group(0))
    return int(digits) if digits else None


def parse_search_results(html: str, category: str) -> list[SearchListing]:
    """Parse listing cards without following their detail-page links."""
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select('[data-marker="item"]')
    listings: list[SearchListing] = []
    seen_ids: set[str] = set()

    for card in cards:
        link = card.select_one('a[data-marker="item-title"][href]')
        if not link:
            link = card.select_one('a[itemprop="url"][href], a[href*="_"][href]')
        if not link:
            continue

        url = urljoin(AVITO_BASE_URL, str(link.get("href"))).split("?")[0]
        listing_id = _extract_id(card, url)
        if not listing_id or listing_id in seen_ids:
            continue

        title = _first_text(
            card,
            ('[data-marker="item-title"]', '[itemprop="name"]', 'h3'),
        )
        price_text = _first_text(
            card,
            ('[data-marker="item-price"]', '[itemprop="price"]'),
        )
        address = _first_text(
            card,
            (
                '[data-marker="item-address"]',
                '[class*="geo-address"]',
                '[class*="address"]',
            ),
        )
        params = _first_text(
            card,
            (
                '[data-marker="item-specific-params"]',
                '[data-marker="item-params"]',
            ),
        )
        published_text = _first_text(
            card,
            ('[data-marker="item-date"]', '[class*="date"]'),
        )
        description = _first_text(
            card,
            ('[data-marker="item-description"]', '[itemprop="description"]'),
        )

        listings.append(
            SearchListing(
                id=listing_id,
                category=category,
                url=url,
                title=title,
                price=_extract_price(card, price_text),
                price_text=price_text,
                address=address,
                params=params,
                published_text=published_text,
                description_snippet=description,
                raw_text=" ".join(card.get_text(" ", strip=True).split()),
            )
        )
        seen_ids.add(listing_id)

    return listings


def page_is_blocked(html: str, current_url: str = "") -> bool:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).lower()
    url = current_url.lower()
    markers = (
        "доступ ограничен",
        "доступ временно ограничен",
        "подтвердите, что вы не робот",
        "пройдите проверку",
        "access denied",
    )
    return "captcha" in url or any(marker in text for marker in markers)
