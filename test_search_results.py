# -*- coding: utf-8 -*-

import unittest

from src.search_results import page_is_blocked, parse_search_results


SEARCH_HTML = """
<html><body>
  <div data-marker="item" data-item-id="1234567890">
    <a data-marker="item-title" href="/essentuki/kvartiry/kvartira_1234567890?context=x">
      2-к. квартира, 55 м², 3/5 эт.
    </a>
    <meta itemprop="price" content="45000">
    <p data-marker="item-price">45 000 ₽ в месяц</p>
    <div data-marker="item-address">Ессентуки, улица Ленина, 10</div>
    <div data-marker="item-specific-params">2 комнаты · 55 м² · 3/5 этаж</div>
    <p data-marker="item-date">сегодня, 10:30</p>
  </div>
</body></html>
"""


class SearchResultsTest(unittest.TestCase):
    def test_extracts_card_without_opening_link(self):
        listings = parse_search_results(SEARCH_HTML, "apartments")
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].id, "1234567890")
        self.assertEqual(listings[0].price, 45000)
        self.assertEqual(listings[0].address, "Ессентуки, улица Ленина, 10")
        self.assertNotIn("?context", listings[0].url)

    def test_detects_block_page(self):
        self.assertTrue(page_is_blocked("<h1>Доступ временно ограничен</h1>"))
        self.assertFalse(page_is_blocked(SEARCH_HTML))


if __name__ == "__main__":
    unittest.main()
