#!/usr/bin/env python3
"""Tests for dense one-dollar Fanatics bands (2026-09-29): split by category, newest-first plus oldest-first past 999,
residual logged. The API count is stubbed -- no network.

  python3 test_price_bands_dense.py
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fanatics_full_catalog_scraper_v3_price_bands as P  # noqa: E402


class DenseShards(unittest.TestCase):
    def setUp(self):
        self.counts = {"A": 0, "B": 500, "C": 1500, "D": 2500}
        self._count, self._cats, self._extra = P.count, P.F.CATEGORIES, P.extra_categories
        P.count = lambda q, lo, hi: self.counts[q["category"]]
        P.F.CATEGORIES = ["A", "B", "C"]
        P.extra_categories = lambda: ["C", "D"]           # overlap with the main list is de-duplicated
        self.logged = []

    def tearDown(self):
        P.count, P.F.CATEGORIES, P.extra_categories = self._count, self._cats, self._extra

    def test_split(self):
        shards = P.dense_shards({}, 1049, 1050, self.logged.append)
        got = [(s["category"], s["sort"]) for s in shards]
        self.assertEqual(got, [("B", "soldDate,desc"), ("C", "soldDate,desc"), ("C", "soldDate,asc"),
                               ("D", "soldDate,desc"), ("D", "soldDate,asc")])
        self.assertTrue(all(s["priceMin"] == 1049 and s["priceMax"] == 1050 for s in shards))
        self.assertEqual(len(self.logged), 1)
        self.assertIn("502 in the middle", self.logged[0])      # D: 2,500 - 2 x 999

    def test_a_category_base_stays_in_its_category(self):
        shards = P.dense_shards({"category": "C"}, 1025, 1026, self.logged.append)
        self.assertEqual([(s["category"], s["sort"]) for s in shards], [("C", "soldDate,desc"), ("C", "soldDate,asc")])


if __name__ == "__main__":
    unittest.main(verbosity=1)
