# -*- coding: utf-8 -*-

import unittest

from src.listing_details import parse_listing_details


DETAIL_HTML = """
<html><head>
  <meta property="og:image" content="https://images.avcdn.net/ip1.jpg">
  <script type="application/ld+json">
    {"image": ["https://images.avcdn.net/ip2.jpg", "https://images.avcdn.net/ip3.jpg"],
     "geo": {"latitude": 44.04, "longitude": 42.86}}
  </script>
</head><body>
  <h1>2-к. квартира, 60 м², 3/5 эт.</h1>
  <span itemprop="price" content="30000"></span>
  <div itemprop="address">Ессентуки, улица Ленина, 10</div>
  <div itemprop="description">Большая квартира на длительный срок</div>
  <div data-marker="item-view/item-params"><ul>
    <li>Количество комнат: 2</li><li>Общая площадь: 60 м²</li>
    <li>Количество жильцов: 4</li><li>Можно с детьми: нет</li>
    <li>Можно с питомцем: нет</li><li>Можно курить: нет</li>
  </ul></div>
  <div data-marker="item-view/seller-info/name">Собственник</div>
</body></html>
"""


class ListingDetailsTest(unittest.TestCase):
    def test_extracts_details_and_first_photos(self):
        details = parse_listing_details(DETAIL_HTML, max_photos=2)
        self.assertEqual(details.price, 30_000)
        self.assertEqual(details.rooms, 2)
        self.assertEqual(details.area, 60)
        self.assertEqual(details.latitude, 44.04)
        self.assertEqual(details.seller, "Собственник")
        self.assertEqual(len(details.photo_urls), 2)
        self.assertIn("Количество жильцов: 4", details.parameters)
        self.assertIn("Можно с детьми: нет", details.parameters)


if __name__ == "__main__":
    unittest.main()
