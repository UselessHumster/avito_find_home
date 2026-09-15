# -*- coding: utf-8 -*-
"""Small Telegram Bot API client for qualified rental notifications."""

from __future__ import annotations

import html
import json
import os
import re
import textwrap
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import requests


class TelegramAPIError(RuntimeError):
    pass


def _address_quality(value: Any) -> tuple[int, int, int]:
    text = str(value or "").strip()
    normalized = re.sub(r"[^0-9a-zа-яё]+", " ", text.casefold()).strip()
    if not normalized:
        return (0, 0, 0)
    street_markers = ("ул", "улица", "пер", "переулок", "шоссе", "пл", "проспект")
    has_street = int(any(re.search(rf"\b{marker}\b", normalized) for marker in street_markers))
    has_house = int(bool(re.search(r"\d", normalized)))
    return (has_street, has_house, len(normalized))


def _best_address(*values: Any) -> str:
    meaningful = [value for value in values if _address_quality(value) > (0, 0, 0)]
    if not meaningful:
        return "Адрес не указан"
    return str(max(meaningful, key=_address_quality))


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without overwriting process variables."""
    if not path.exists():
        return
    # PowerShell 5 writes UTF-8 files with BOM by default. ``utf-8-sig``
    # transparently accepts both forms, preventing the first variable name
    # (usually TELEGRAM_BOT_TOKEN) from becoming invisible to the parser.
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def build_listing_message(listing: dict[str, Any], details: dict[str, Any] | None) -> str:
    details = details or {}
    score = int(listing.get("score") or 0)
    title = listing.get("title") or details.get("title") or "Объявление"
    price = listing.get("price") or details.get("price")
    area = listing.get("area") or details.get("area")
    rooms = listing.get("rooms") or details.get("rooms")
    # The detail page can contain a fuller address (including the house number)
    # than the search card, so prefer it when it is available.
    address = _best_address(details.get("address"), listing.get("address"))
    seller = details.get("seller")
    url = listing.get("url") or ""

    try:
        reasons = json.loads(listing.get("score_reasons") or "{}")
    except (json.JSONDecodeError, TypeError):
        reasons = {}
    parameters = details.get("parameters")
    normalized_parameters = (
        [re.sub(r"\s+:\s*", ": ", str(value)).strip() for value in parameters]
        if isinstance(parameters, list)
        else []
    )
    children_answer = None
    for value in normalized_parameters:
        normalized_rule = value.casefold().replace("ё", "е")
        if normalized_rule.startswith("можно с детьми:"):
            children_answer = normalized_rule.split(":", 1)[1].strip()
            break
    other_utilities = reasons.get("other_utilities")
    effective_price = reasons.get("effective_price")
    green_price_max = int(reasons.get("green_price_max", 30_000))
    maximum_price = int(reasons.get("maximum_price", 40_000))
    area_points = int(reasons.get("area_points", 1))
    price_points = int(reasons.get("price_points", 1))
    maximum_score = int(reasons.get("maximum_score", 4))
    show_children = bool(reasons.get("show_children", True))
    show_commission = bool(reasons.get("show_commission", True))
    show_utilities = bool(reasons.get("show_utilities", True))
    price_note = ""
    if effective_price is not None:
        formatted_price = f"{int(effective_price):,}".replace(",", " ")
        price_note = f"{formatted_price} ₽/месяц"
        if other_utilities is None:
            price_note += " (ЖКУ пока не учтены)"

    lines = [
        f"🏠 <b>{html.escape(str(title))}</b>",
        f"⭐ <b>Рейтинг: {score}/{maximum_score} баллов</b>",
        "",
        "<b>Важное</b>",
    ]
    if show_children and children_answer in {"нет", "нельзя", "запрещено"}:
        lines.append("🔴 <b>С детьми нельзя</b> (на рейтинг не влияет)")
    if reasons.get("negative_area"):
        street_points = f"{int(reasons.get('street_score', -1)):+d}".replace("-", "−")
        lines.append(f"🔴 Нежелательная улица: {street_points} балл")
    if effective_price is not None and effective_price > maximum_price:
        lines.append(f"🔴 Цена {price_note}: +0 баллов")
    if show_commission and reasons.get("commission_present"):
        lines.append("🟡 <b>Есть комиссия</b> (на рейтинг не влияет)")
    if effective_price is not None and green_price_max < effective_price <= maximum_price:
        lines.append(f"🟡 Цена {price_note}: +{price_points} балл")
    if reasons.get("area_54_plus"):
        lines.append(f"🟢 Площадь от 54 м²: +{area_points} балл")
    if effective_price is not None and effective_price <= green_price_max:
        lines.append(f"🟢 Цена {price_note}: +{price_points} балл")
    if reasons.get("preferred_area"):
        lines.append(f"🟢 Предпочтительная улица: {int(reasons.get('street_score', 2)):+d} балла")
    elif not reasons.get("negative_area"):
        lines.append(f"🟢 Нейтральная улица: {int(reasons.get('street_score', 1)):+d} балл")
    if show_commission and reasons.get("no_commission"):
        lines.append("🟢 Без комиссии (на рейтинг не влияет)")
    if show_children and children_answer in {"да", "можно", "разрешено"}:
        lines.append("🟢 <b>Можно с детьми</b> (на рейтинг не влияет)")
    if show_children and children_answer is None:
        lines.append("⚪ Можно ли с детьми: пока неизвестно")
    if effective_price is None:
        lines.append("⚪ Цена: пока неизвестна")
    if show_utilities and reasons.get("other_utilities") is None:
        lines.append("⚪ Размер ЖКУ: пока неизвестен")
    if show_commission and not reasons.get("commission_present") and not reasons.get("no_commission"):
        lines.append("⚪ Комиссия: пока неизвестна")
    lines.extend([
        "",
        "<b>Основное</b>",
    ])
    if effective_price is not None and other_utilities is not None:
        lines.append(
            f"💰 Реальная цена: <b>{int(effective_price):,} ₽/месяц</b> "
            f"(ЖКУ {int(other_utilities):,} ₽)".replace(",", " ")
        )
    elif price is not None:
        lines.append(
            f"💰 Цена: <b>{int(price):,} ₽/месяц</b> (ЖКУ пока не учтены)".replace(",", " ")
        )
    if area is not None:
        lines.append(f"📐 Площадь: <b>{float(area):g} м²</b>")
    if rooms is not None:
        lines.append(f"🚪 Комнат: <b>{int(rooms)}</b>")
    lines.append(f"📍 Адрес: {html.escape(str(address))}")
    if seller:
        lines.append(f"👤 Продавец: {html.escape(str(seller))}")

    if normalized_parameters:
        rule_prefixes = (
            "количество жильцов:",
            "можно с детьми:",
            "можно с питомцем:",
            "можно курить:",
        )
        rules = [
            value
            for value in normalized_parameters
            if value.casefold().startswith(rule_prefixes)
        ]
        other_parameters = [value for value in normalized_parameters if value not in rules]

        if other_parameters:
            lines.extend(("", "<b>Параметры объявления</b>"))
            lines.extend(f"• {html.escape(value)}" for value in other_parameters)
        if rules:
            lines.extend(("", "<b>Правила</b>"))
            for value in rules:
                normalized_rule = value.casefold().replace("ё", "е")
                if normalized_rule.startswith("можно с детьми:"):
                    continue
                else:
                    lines.append(f"• {html.escape(value)}")

    latitude = details.get("latitude")
    longitude = details.get("longitude")
    if latitude is not None and longitude is not None:
        map_url = f"https://www.google.com/maps?q={latitude},{longitude}"
        lines.extend(
            (
                "",
                "<b>Расположение</b>",
                f"🗺 Координаты: {html.escape(str(latitude))}, {html.escape(str(longitude))}",
                f'📌 <a href="{html.escape(map_url, quote=True)}">Открыть на карте</a>',
            )
        )

    description = details.get("description")
    if description:
        compact = " ".join(str(description).split())
        # Keep the complete description, but introduce safe line boundaries so
        # long Telegram messages can be split without cutting HTML entities.
        description_lines = textwrap.wrap(
            compact,
            width=900,
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.extend(("", "<b>Описание</b>"))
        lines.extend(html.escape(line) for line in description_lines)
    if url:
        lines.extend(
            (
                "",
                f'🔗 <a href="{html.escape(str(url), quote=True)}"><b>Открыть на Авито</b></a>',
            )
        )
    return "\n".join(lines)


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split at line boundaries so Telegram HTML tags are never cut in half."""
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in text.splitlines():
        added_length = len(line) + (1 if current else 0)
        if current and current_length + added_length > limit:
            chunks.append("\n".join(current))
            current = []
            current_length = 0
        current.append(line)
        current_length += len(line) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        timeout: float = 30,
        session: requests.Session | None = None,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat ID are required")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._chat_id = chat_id
        self._timeout = timeout
        self._session = session or requests.Session()

    def _call(
        self,
        method: str,
        *,
        data: dict[str, Any],
        files: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._session.post(
                f"{self._base_url}/{method}",
                data=data,
                files=files,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise TelegramAPIError(
                f"Telegram API request failed: {type(exc).__name__}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramAPIError(f"Telegram API returned HTTP {response.status_code}") from exc
        if not response.ok or not payload.get("ok"):
            description = str(payload.get("description") or "request rejected")
            raise TelegramAPIError(f"Telegram API: {description}")
        return payload.get("result")

    @staticmethod
    def _message_ids(result: Any) -> list[int]:
        messages = result if isinstance(result, list) else [result]
        return [
            int(message["message_id"])
            for message in messages
            if isinstance(message, dict) and message.get("message_id") is not None
        ]

    def send_listing(self, text: str, photo_paths: list[Path]) -> list[int]:
        existing_photos = [path for path in photo_paths[:3] if path.is_file()]
        message_ids: list[int] = []
        for text_chunk in split_message(text):
            text_result = self._call(
                "sendMessage",
                data={
                    "chat_id": self._chat_id,
                    "text": text_chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
            message_ids.extend(self._message_ids(text_result))
        if not existing_photos:
            return message_ids

        if len(existing_photos) == 1:
            with existing_photos[0].open("rb") as photo:
                result = self._call(
                    "sendPhoto",
                    data={
                        "chat_id": self._chat_id,
                        "caption": "📸 <b>Фотография объявления</b>",
                        "parse_mode": "HTML",
                        "reply_parameters": json.dumps({"message_id": message_ids[0]}),
                    },
                    files={"photo": photo},
                )
            return [*message_ids, *self._message_ids(result)]

        with ExitStack() as stack:
            files = {
                f"photo{index}": stack.enter_context(path.open("rb"))
                for index, path in enumerate(existing_photos)
            }
            media = []
            for index in range(len(existing_photos)):
                item = {"type": "photo", "media": f"attach://photo{index}"}
                if index == 0:
                    item.update({"caption": "📸 Фотографии объявления"})
                media.append(item)
            result = self._call(
                "sendMediaGroup",
                data={
                    "chat_id": self._chat_id,
                    "media": json.dumps(media, ensure_ascii=False),
                    "reply_parameters": json.dumps({"message_id": message_ids[0]}),
                },
                files=files,
            )
        return [*message_ids, *self._message_ids(result)]

    def enrich_listing(
        self,
        text: str,
        original_message_ids: list[int],
        photo_paths: list[Path],
    ) -> list[int]:
        if not original_message_ids:
            return self.send_listing(text, photo_paths)
        chunks = split_message(text)
        result = self._call(
            "editMessageText",
            data={
                "chat_id": self._chat_id,
                "message_id": str(original_message_ids[0]),
                "text": chunks[0],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        message_ids = self._message_ids(result) or [original_message_ids[0]]
        for chunk in chunks[1:]:
            extra = self._call(
                "sendMessage",
                data={
                    "chat_id": self._chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
            message_ids.extend(self._message_ids(extra))
        message_ids.extend(self._send_photos(photo_paths, original_message_ids[0]))
        return message_ids

    def edit_listing_text(self, text: str, message_id: int) -> list[int]:
        chunks = split_message(text)
        result = self._call(
            "editMessageText",
            data={
                "chat_id": self._chat_id,
                "message_id": str(message_id),
                "text": chunks[0],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )
        message_ids = self._message_ids(result) or [message_id]
        for chunk in chunks[1:]:
            extra = self._call(
                "sendMessage",
                data={
                    "chat_id": self._chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                    "reply_parameters": json.dumps({"message_id": message_id}),
                },
            )
            message_ids.extend(self._message_ids(extra))
        return message_ids

    def _send_photos(self, photo_paths: list[Path], reply_to_message_id: int) -> list[int]:
        existing_photos = [path for path in photo_paths[:3] if path.is_file()]
        if not existing_photos:
            return []
        if len(existing_photos) == 1:
            with existing_photos[0].open("rb") as photo:
                result = self._call(
                    "sendPhoto",
                    data={
                        "chat_id": self._chat_id,
                        "caption": "📸 <b>Фотография объявления</b>",
                        "parse_mode": "HTML",
                        "reply_parameters": json.dumps({"message_id": reply_to_message_id}),
                    },
                    files={"photo": photo},
                )
            return self._message_ids(result)
        with ExitStack() as stack:
            files = {
                f"photo{index}": stack.enter_context(path.open("rb"))
                for index, path in enumerate(existing_photos)
            }
            media = []
            for index in range(len(existing_photos)):
                item = {"type": "photo", "media": f"attach://photo{index}"}
                if index == 0:
                    item["caption"] = "📸 Фотографии объявления"
                media.append(item)
            result = self._call(
                "sendMediaGroup",
                data={
                    "chat_id": self._chat_id,
                    "media": json.dumps(media, ensure_ascii=False),
                    "reply_parameters": json.dumps({"message_id": reply_to_message_id}),
                },
                files=files,
            )
        return self._message_ids(result)

    def send_alert(self, title: str, message: str) -> list[int]:
        text = (
            f"🚨 <b>{html.escape(title)}</b>\n\n"
            f"{html.escape(message)}"
        )
        message_ids: list[int] = []
        for chunk in split_message(text):
            result = self._call(
                "sendMessage",
                data={
                    "chat_id": self._chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
            )
            message_ids.extend(self._message_ids(result))
        return message_ids

    def delete_messages(self, message_ids: list[int]) -> None:
        for message_id in message_ids:
            self._call(
                "deleteMessage",
                data={"chat_id": self._chat_id, "message_id": str(message_id)},
            )
