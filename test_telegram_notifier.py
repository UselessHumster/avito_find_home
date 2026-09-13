# -*- coding: utf-8 -*-

import json
import tempfile
import unittest
from pathlib import Path

from src.telegram_notifier import TelegramNotifier, build_listing_message, split_message


class FakeResponse:
    ok = True
    status_code = 200

    def __init__(self, result):
        self._result = result

    def json(self):
        return {"ok": True, "result": self._result}


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, data, files, timeout):
        self.calls.append({"url": url, "data": data, "files": files, "timeout": timeout})
        if url.endswith("/sendMediaGroup"):
            media = json.loads(data["media"])
            return FakeResponse([{"message_id": index + 1} for index in range(len(media))])
        return FakeResponse({"message_id": 1})


class TelegramNotifierTest(unittest.TestCase):
    def test_builds_safe_ranked_message(self):
        message = build_listing_message(
            {
                "score": 3,
                "title": "2-к. квартира <центр>",
                "price": 30_000,
                "area": 54,
                "rooms": 2,
                "address": "ул. Ленина, 10",
                "url": "https://www.avito.ru/item/1",
                "score_reasons": json.dumps(
                    {
                        "area_54_plus": True,
                        "no_commission": True,
                        "price_40000_or_less": True,
                        "effective_price": 30000,
                        "commission_present": False,
                    }
                ),
            },
            {
                "description": "Хорошая квартира",
                "seller": "Собственник",
                "latitude": 44.04,
                "longitude": 42.86,
                "parameters": [
                    "Залог: 10 000 ₽",
                    "Комиссия: 0 %",
                    "Количество жильцов : 4",
                    "Можно с детьми : нет",
                    "Можно с питомцем : нет",
                    "Можно курить : нет",
                ],
            },
        )
        self.assertIn("Рейтинг: 3/4 баллов", message)
        self.assertIn("&lt;центр&gt;", message)
        self.assertIn("• Залог: 10 000 ₽\n• Комиссия: 0 %", message)
        self.assertIn("<b>Основное</b>", message)
        self.assertIn("<b>Важное</b>", message)
        self.assertIn("📍 Адрес: ул. Ленина, 10", message)
        self.assertIn("👤 Продавец: Собственник", message)
        self.assertIn("<b>Правила</b>", message)
        self.assertIn("• Количество жильцов: 4", message)
        self.assertIn("🔴 <b>С детьми нельзя</b>", message)
        self.assertIn("на рейтинг не влияет", message)
        self.assertIn("• Можно с питомцем: нет", message)
        self.assertIn("• Можно курить: нет", message)
        self.assertIn("<b>Расположение</b>", message)
        self.assertLessEqual(len(message), 4096)

    def test_ignores_punctuation_only_detail_address(self):
        message = build_listing_message(
            {"address": "ул. Буачидзе, 1к2"},
            {"address": ","},
        )
        self.assertIn("📍 Адрес: ул. Буачидзе, 1к2", message)

    def test_price_uses_requested_color_tiers(self):
        green = build_listing_message(
            {"score_reasons": json.dumps({"effective_price": 30_000})}, None
        )
        yellow = build_listing_message(
            {"score_reasons": json.dumps({"effective_price": 40_000})}, None
        )
        red = build_listing_message(
            {"score_reasons": json.dumps({"effective_price": 45_000})}, None
        )
        self.assertIn("🟢 Цена 30 000 ₽/месяц", green)
        self.assertIn("🟡 Цена 40 000 ₽/месяц", yellow)
        self.assertIn("🔴 Цена 45 000 ₽/месяц", red)

    def test_shows_negative_street_score(self):
        message = build_listing_message(
            {
                "score": 2,
                "title": "2-к. квартира, 60 м²",
                "address": "ул. Ермолова, 10",
                "score_reasons": json.dumps({"negative_area": True}),
            },
            None,
        )
        self.assertIn("Рейтинг: 2/4 баллов", message)
        self.assertIn("Нежелательная улица: −1 балл", message)

    def test_keeps_complete_long_description(self):
        description = "Очень подробное описание " * 100
        message = build_listing_message({}, {"description": description})
        self.assertEqual(message.count("Очень подробное описание"), 100)

    def test_sends_three_photos_as_media_group(self):
        fake_session = FakeSession()
        notifier = TelegramNotifier("secret-token", "123", session=fake_session)
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index in range(3):
                path = Path(directory) / f"{index}.jpg"
                path.write_bytes(b"image")
                paths.append(path)
            message_ids = notifier.send_listing("test", paths)

        self.assertEqual(len(message_ids), 4)
        self.assertTrue(fake_session.calls[0]["url"].endswith("/sendMessage"))
        self.assertTrue(fake_session.calls[1]["url"].endswith("/sendMediaGroup"))
        self.assertEqual(len(json.loads(fake_session.calls[1]["data"]["media"])), 3)

    def test_sends_text_when_photos_are_missing(self):
        fake_session = FakeSession()
        notifier = TelegramNotifier("secret-token", "123", session=fake_session)
        self.assertEqual(notifier.send_listing("test", []), [1])
        self.assertTrue(fake_session.calls[0]["url"].endswith("/sendMessage"))

    def test_deletes_saved_message_ids(self):
        fake_session = FakeSession()
        notifier = TelegramNotifier("secret-token", "123", session=fake_session)
        notifier.delete_messages([10, 11])
        self.assertEqual(len(fake_session.calls), 2)
        self.assertTrue(all(call["url"].endswith("/deleteMessage") for call in fake_session.calls))

    def test_edits_original_message_when_details_arrive(self):
        fake_session = FakeSession()
        notifier = TelegramNotifier("secret-token", "123", session=fake_session)
        message_ids = notifier.enrich_listing("Полная карточка", [77], [])
        self.assertEqual(message_ids, [1])
        self.assertTrue(fake_session.calls[0]["url"].endswith("/editMessageText"))
        self.assertEqual(fake_session.calls[0]["data"]["message_id"], "77")

    def test_sends_escaped_operational_alert(self):
        fake_session = FakeSession()
        notifier = TelegramNotifier("secret-token", "123", session=fake_session)
        self.assertEqual(notifier.send_alert("Капча <Авито>", "Нужен вход & проверка"), [1])
        payload = fake_session.calls[0]["data"]["text"]
        self.assertIn("Капча &lt;Авито&gt;", payload)
        self.assertIn("вход &amp; проверка", payload)

    def test_splits_long_text_only_between_lines(self):
        original = ("<b>Заголовок</b>\n" + "строка\n" * 20).rstrip("\n")
        chunks = split_message(original, limit=40)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("\n".join(chunks), original)


if __name__ == "__main__":
    unittest.main()
