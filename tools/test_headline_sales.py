#!/usr/bin/env python3
"""Tests for tools/headline_sales.py: title parsing and the same-sale merge rule. No database, no network.

  python3 tools/test_headline_sales.py
"""
import json, os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import headline_sales as H  # noqa: E402


def row(src, price, day, title, prec="day", venue="fanatics", source_id=None):
    return {"src": src, "venue": venue, "sale_type": "auction", "source_id": source_id, "title": title, "price": price,
            "date": day, "prec": prec, "url": None}


class ParseTitle(unittest.TestCase):
    def test_flagg_debut_patch(self):
        p = H.parse_title("2025 Topps Chrome Update Cooper Flagg Rookie NBA Debut Patch Auto 1/1 #DPA-CF PSA 10")
        self.assertEqual((p["year"], p["code"], p["code_key"], p["serial"], p["print_run"], p["grade"]),
                         (2025, "DPA-CF", "dpacf", "1/1", 1, "PSA 10"))

    def test_card_number_is_not_a_serial(self):
        p = H.parse_title("2003-04 Upper Deck Exquisite Collection Rookie Patch Autograph #78 /99 LeBron James BGS 9.5")
        self.assertEqual((p["year"], p["code"], p["serial"], p["print_run"], p["grade"]), (2003, "78", None, 99, "BGS 9.5"))
        p = H.parse_title("2003-04 Exquisite #78/99 LeBron James")
        self.assertEqual((p["code"], p["serial"], p["print_run"]), ("78", None, 99))

    def test_bare_print_run(self):
        p = H.parse_title("2003-04 Upper Deck Exquisite Collection Rookie Patch Autograph #78 LeBron James /23 BGS 9.5")
        self.assertEqual((p["code"], p["serial"], p["print_run"]), ("78", None, 23))

    def test_numbered_copy(self):
        p = H.parse_title("1997-98 Metal Universe Precious Metal Gems Green 05/10 #81 Kobe Bryant PSA 5")
        self.assertEqual((p["serial"], p["print_run"], p["code"], p["grade"]), ("05/10", 10, "81", "PSA 5"))

    def test_nothing_to_read(self):
        p = H.parse_title("Shohei Ohtani Logoman")
        self.assertEqual((p["year"], p["code"], p["serial"], p["print_run"], p["grade"]), (None, None, None, None, None))


class SameSale(unittest.TestCase):
    def setUp(self):
        self.a = row("fanatics_api", 8_040_000, "2026-09-25", "2025 Topps Chrome Update Cooper Flagg Debut Patch 1/1")
        self.b = row("seed", 8_040_000, "2026-09-26", "Cooper Flagg NBA Debut Patch Auto 1/1 PSA 10", venue="fanatics")
        self.players = {id(self.a): {"cooperflagg"}, id(self.b): {"cooperflagg"}}

    def test_same_player_price_and_days(self):
        self.assertTrue(H.same_sale(self.a, self.b, self.players))

    def test_price_more_than_one_percent_apart(self):
        self.b["price"] = 8_040_000 * 1.02
        self.assertFalse(H.same_sale(self.a, self.b, self.players))

    def test_price_within_one_percent(self):
        self.b["price"] = 8_040_000 * 1.009          # premium rounding in a press report
        self.assertTrue(H.same_sale(self.a, self.b, self.players))

    def test_more_than_three_days_apart(self):
        self.b["date"] = "2026-09-29"
        self.assertFalse(H.same_sale(self.a, self.b, self.players))

    def test_month_precision(self):
        self.b.update(date="2026-09-01", prec="month")
        self.assertTrue(H.same_sale(self.a, self.b, self.players))
        self.b["date"] = "2026-08-01"
        self.assertFalse(H.same_sale(self.a, self.b, self.players))

    def test_different_players_never_merge(self):
        self.players[id(self.b)] = {"dylanharper"}
        self.assertFalse(H.same_sale(self.a, self.b, self.players))

    def test_no_player_read_falls_back_to_title_overlap(self):
        self.players = {}
        self.assertTrue(H.same_sale(self.a, self.b, self.players))
        self.b["title"] = "Mystery lot one of one"
        self.assertFalse(H.same_sale(self.a, self.b, self.players))


class Canonicalize(unittest.TestCase):
    def test_feed_and_press_merge_into_one_record(self):
        feed = row("fanatics_api", 8_040_000, "2026-09-25", "2025 Topps Chrome Update Cooper Flagg #DPA-CF 1/1 PSA 10", source_id="F1")
        press = row("seed", 8_040_000, "2026-09-01", "Cooper Flagg Debut Patch 1/1", prec="month", venue="private")
        other = row("seed", 2_880_000, "2026-09-25", "Dylan Harper Debut Patch 1/1")
        players = {id(feed): {"cooperflagg"}, id(press): {"cooperflagg"}, id(other): {"dylanharper"}}
        out = H.canonicalize([press, other, feed], players)
        self.assertEqual(len(out), 2)
        flagg = next(s for s in out if s["price"] == 8_040_000)
        self.assertEqual((flagg["venue"], flagg["date"], flagg["code"], len(flagg["sources"])), ("fanatics", "2026-09-25", "DPA-CF", 2))

    def test_same_venue_id_is_one_sale(self):
        a = row("fanatics_api", 150_000, "2026-09-25", "A", source_id="F9")
        b = row("neon", 150_000, "2026-09-25", "A", source_id="F9")
        self.assertEqual(len(H.canonicalize([a, b], {})), 1)


class Seed(unittest.TestCase):
    def test_seed_file_is_well_formed(self):
        with open(H.SEED) as f:
            d = json.load(f)
        self.assertGreaterEqual(len(d["sales"]), 20)
        for s in d["sales"]:
            self.assertGreaterEqual(float(s["price"]), 1_000_000, s["title"])
            self.assertIn(s["prec"], ("day", "month"))
            self.assertIn(s["source"], d["sources"], s["title"])
            H.date.fromisoformat(s["date"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
