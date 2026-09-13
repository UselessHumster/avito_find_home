# -*- coding: utf-8 -*-
"""Extraction of detail-page data after a listing passes the ranking gate."""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .listing_rules import extract_area, extract_rooms


AVITO_BASE_URL = "https://www.avito.ru"


@dataclass
class ListingDetails:
    title: str | None
    price: int | None
    description: str | None
    address: str | None
    seller: str | None
    rooms: int | None
    area: float | None
    parameters: list[str]
    latitude: float | None
    longitude: float | None
    photo_urls: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = " ".join(element.get_text(" ", strip=True).split())
            if text:
                return text
    return None


def _extract_price(soup: BeautifulSoup) -> int | None:
    element = soup.select_one('[itemprop="price"], [data-marker*="price"]')
    if not element:
        return None
    value = element.get("content") or element.get_text(" ", strip=True)
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _extract_parameters(soup: BeautifulSoup) -> list[str]:
    containers = soup.select(
        '[data-marker="item-view/item-params"], '
        '[data-marker*="item-params"], '
        '[class*="params-paramsList"]'
    )
    values: list[str] = []
    seen: set[str] = set()
    for container in containers:
        items = container.select("li") or [container]
        for item in items:
            text = " ".join(item.get_text(" ", strip=True).split())
            if text and text not in seen:
                seen.add(text)
                values.append(text)
    return values


def _extract_coordinates(soup: BeautifulSoup) -> tuple[float | None, float | None]:
    map_element = soup.find(attrs={"data-map-lat": True, "data-map-lon": True})
    if isinstance(map_element, Tag):
        try:
            return float(str(map_element["data-map-lat"])), float(str(map_element["data-map-lon"]))
        except (KeyError, TypeError, ValueError):
            pass

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        objects = payload if isinstance(payload, list) else [payload]
        for item in objects:
            if not isinstance(item, dict) or not isinstance(item.get("geo"), dict):
                continue
            try:
                return float(item["geo"]["latitude"]), float(item["geo"]["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
    return None, None


def _photo_candidates(soup: BeautifulSoup) -> list[str]:
    candidates: list[str] = []

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        objects = payload if isinstance(payload, list) else [payload]
        for item in objects:
            if not isinstance(item, dict):
                continue
            images = item.get("image", [])
            if isinstance(images, str):
                images = [images]
            if isinstance(images, list):
                candidates.extend(str(image) for image in images if image)

    for meta in soup.select('meta[property="og:image"][content]'):
        candidates.append(str(meta.get("content")))

    for image in soup.select('img[src], img[data-src], img[srcset], source[srcset]'):
        for attribute in ("src", "data-src"):
            if image.get(attribute):
                candidates.append(str(image.get(attribute)))
        if image.get("srcset"):
            candidates.extend(
                item.strip().split(" ", 1)[0]
                for item in str(image.get("srcset")).split(",")
                if item.strip()
            )

    decoded_html = html_module.unescape(str(soup)).replace(r"\u002F", "/").replace(r"\/", "/")
    candidates.extend(
        match.rstrip("\\")
        for match in re.findall(
            r"https?://[^\"'\s<>]+(?:avcdn|image)[^\"'\s<>]*",
            decoded_html,
        )
    )

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = urljoin(AVITO_BASE_URL, candidate.strip())
        lowered = url.lower()
        if not url.startswith(("http://", "https://")) or url.startswith("data:"):
            continue
        if not any(marker in lowered for marker in ("avcdn", "image", "photo", "img")):
            continue
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def parse_listing_details(html: str, max_photos: int = 3) -> ListingDetails:
    soup = BeautifulSoup(html, "lxml")
    title = _first_text(soup, ('[data-marker="item-view/title-info"] h1', "h1"))
    description = _first_text(
        soup,
        ('[itemprop="description"]', '[data-marker*="description"]'),
    )
    address = _first_text(
        soup,
        ('[itemprop="address"]', '[data-marker*="address"]'),
    )
    seller = _first_text(
        soup,
        ('[data-marker*="seller-info/name"]', '[data-marker*="seller-info"]'),
    )
    parameters = _extract_parameters(soup)
    parameter_text = " ".join(parameters)
    latitude, longitude = _extract_coordinates(soup)
    photos = _photo_candidates(soup)

    return ListingDetails(
        title=title,
        price=_extract_price(soup),
        description=description,
        address=address,
        seller=seller,
        rooms=extract_rooms(parameter_text, title),
        area=extract_area(parameter_text, title),
        parameters=parameters,
        latitude=latitude,
        longitude=longitude,
        photo_urls=photos[:max(0, max_photos)],
    )
