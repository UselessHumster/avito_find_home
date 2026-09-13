# -*- coding: utf-8 -*-

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from semi_manual_collector import (
    load_config,
    record_telegram_result,
    save_listing_details,
    save_listings,
    select_detail_candidates,
    select_notification_candidates,
)
from src.listing_details import ListingDetails
from src.search_results import SearchListing


class SemiManualCollectorStorageTest(unittest.TestCase):
    def test_loads_public_yaml_configuration(self):
        config = load_config(Path(__file__).with_name("config.yaml"))

        self.assertEqual(config["city"]["name"], "Ессентуки")
        self.assertEqual(config["filters"]["minimum_rooms"], 2)
        self.assertEqual(config["ranking"]["maximum_price"], 40_000)
        self.assertEqual(config["ranking"]["preferred_street_points"], 2)

    def test_low_score_listing_is_notified_without_opening_detail_page(self):
        listing = SearchListing(
            id="low-score",
            category="apartments",
            url="https://www.avito.ru/essentuki/kvartiry/low-score",
            title="2-к. квартира, 45 м², 2/5 эт.",
            price=35_000,
            price_text="35 000 ₽ в месяц",
            address="Тихая ул., 7",
            params="Без комиссии",
            published_text="сегодня",
            description_snippet=None,
            raw_text="Сдаётся на длительный срок",
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "listings.sqlite3"
            save_listings(
                database,
                [listing],
                completed=False,
                category="apartments",
                filters={"minimum_rooms": 2, "exclude_short_term": True},
                ranking={"minimum_area": 54, "maximum_price": 30_000},
            )
            notifications = select_notification_candidates(database, minimum_score=3, limit=10)
            self.assertEqual([candidate["id"] for candidate in notifications], ["low-score"])
            self.assertEqual(notifications[0]["detail_status"], "not_requested")

    def test_saves_evaluation_and_selects_three_point_candidate(self):
        listing = SearchListing(
            id="123",
            category="apartments",
            url="https://www.avito.ru/essentuki/kvartiry/test_123",
            title="2-к. квартира, 54 м², 3/5 эт.",
            price=30_000,
            price_text="30 000 ₽ в месяц",
            address="ул. Ленина, 10",
            params="Без комиссии",
            published_text="сегодня",
            description_snippet=None,
            raw_text="Сдаётся на длительный срок",
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "listings.sqlite3"
            save_listings(
                database,
                [listing],
                completed=False,
                category="apartments",
                filters={"minimum_rooms": 2, "exclude_short_term": True},
                ranking={
                    "minimum_area": 54,
                    "maximum_price": 30_000,
                    "preferred_address_patterns": [],
                },
            )
            with closing(sqlite3.connect(database)) as connection:
                row = connection.execute(
                    "SELECT rooms, area, hard_filter_passed, score FROM listings"
                ).fetchone()
            self.assertEqual(row, (2, 54.0, 1, 3))
            candidates = select_detail_candidates(database, minimum_score=3, limit=3)
            self.assertEqual([candidate["id"] for candidate in candidates], ["123"])
            immediate = select_notification_candidates(database, 3, 10)
            self.assertEqual([candidate["id"] for candidate in immediate], ["123"])

            save_listing_details(
                database,
                "123",
                ListingDetails(
                    title=listing.title,
                    price=listing.price,
                    description="Описание",
                    address=listing.address,
                    seller="Собственник",
                    rooms=2,
                    area=54,
                    parameters=[],
                    latitude=None,
                    longitude=None,
                    photo_urls=[],
                ),
                photo_paths=[],
                filters={"minimum_rooms": 2, "exclude_short_term": True},
                ranking={
                    "minimum_area": 54,
                    "maximum_price": 30_000,
                    "preferred_address_patterns": [],
                },
            )
            notifications = select_notification_candidates(database, minimum_score=3, limit=10)
            self.assertEqual([candidate["id"] for candidate in notifications], ["123"])
            record_telegram_result(database, "123", message_ids=[10, 11, 12])
            self.assertEqual(select_notification_candidates(database, 3, 10), [])


if __name__ == "__main__":
    unittest.main()
