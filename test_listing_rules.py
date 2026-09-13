# -*- coding: utf-8 -*-

import unittest

from src.listing_rules import evaluate_listing, extract_other_utilities, is_short_term_rental


class ListingRulesTest(unittest.TestCase):
    def evaluate(self, **overrides):
        values = {
            "category": "apartments",
            "title": "2-к. квартира, 54 м², 3/5 эт.",
            "price": 30_000,
            "price_text": "30 000 ₽ в месяц",
            "address": "ул. Ленина, 10",
            "params": "Без комиссии",
            "raw_text": "Сдаётся на длительный срок",
        }
        values.update(overrides)
        return evaluate_listing(**values)

    def test_matching_apartment_scores_three(self):
        result = self.evaluate()
        self.assertTrue(result.hard_filter_passed)
        self.assertEqual(result.rooms, 2)
        self.assertEqual(result.area, 54)
        self.assertEqual(result.score, 3)

    def test_one_room_apartment_is_filtered(self):
        result = self.evaluate(title="1-к. квартира, 60 м², 3/5 эт.")
        self.assertFalse(result.hard_filter_passed)
        self.assertEqual(result.filter_reason, "not_enough_rooms")

    def test_short_term_listing_is_filtered(self):
        result = self.evaluate(price_text="3 000 ₽ за 1 сутки", raw_text="Посуточная аренда")
        self.assertFalse(result.hard_filter_passed)
        self.assertTrue(result.is_short_term)

    def test_negative_short_term_phrase_is_not_short_term(self):
        self.assertFalse(is_short_term_rental(None, "Не сдаётся посуточно, только на длительный срок"))

    def test_house_waits_for_detail_room_count(self):
        result = self.evaluate(
            category="houses_cottages",
            title="Дом 90 м² на участке 3 сот.",
        )
        self.assertTrue(result.hard_filter_passed)
        self.assertTrue(result.room_check_pending)

        rejected = self.evaluate(
            category="houses_cottages",
            title="Дом 90 м² на участке 3 сот.",
            detail_rooms=1,
        )
        self.assertFalse(rejected.hard_filter_passed)

    def test_house_can_get_room_count_from_visible_snippet(self):
        result = self.evaluate(
            category="houses_cottages",
            title="Дом 90 м² на участке 3 сот.",
            raw_text="В доме гостиная и 3 спальни",
        )
        self.assertEqual(result.rooms, 3)
        self.assertFalse(result.room_check_pending)

    def test_preferred_area_adds_one_point(self):
        result = self.evaluate(preferred_address_patterns=["Ленина"])
        self.assertTrue(result.preferred_area_match)
        self.assertEqual(result.score, 4)

    def test_negative_area_subtracts_one_point(self):
        result = self.evaluate(negative_address_patterns=["Ленина"])
        self.assertTrue(result.negative_area_match)
        self.assertEqual(result.score, 1)

    def test_commission_does_not_change_score(self):
        without = self.evaluate(params="Без комиссии")
        with_commission = self.evaluate(params="Комиссия 50%")
        self.assertEqual(without.score, with_commission.score)
        self.assertTrue(with_commission.score_reasons["commission_present"])

    def test_other_utilities_are_added_to_effective_price(self):
        result = self.evaluate(
            price=38_000,
            maximum_price=40_000,
            detail_parameters=["Другие ЖКУ : 3 000 ₽/мес."],
        )
        self.assertEqual(extract_other_utilities(["Другие ЖКУ : 3 000 ₽/мес."]), 3000)
        self.assertEqual(result.effective_price, 41_000)
        self.assertFalse(result.score_reasons["price_40000_or_less"])
        self.assertEqual(result.score, 2)

    def test_address_matching_ignores_case_and_punctuation(self):
        result = self.evaluate(
            address="ул. Большая Боргустанская, 49",
            preferred_address_patterns=["Большая Боргустанская"],
        )
        self.assertTrue(result.preferred_area_match)

    def test_explicit_title_rooms_win_over_studio_in_description(self):
        result = self.evaluate(
            title="4-к. дом, 120 м²",
            raw_text="Рядом находится творческая студия",
        )
        self.assertEqual(result.rooms, 4)


if __name__ == "__main__":
    unittest.main()
