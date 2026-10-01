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

    def test_vintage_grade_descriptors(self):
        for title, want in [("1952 Topps #311 Mickey Mantle - SGC NM+ 7.5", "SGC 7.5"), ("1952 Topps Mickey Mantle #311 PSA EX-MT 6.", "PSA 6"),
                            ("1952 Topps Mickey Mantle #311 PSA Good 2.", "PSA 2"), ("1997-98 Metal Universe PMG #23 Jordan (#018/50) - BGS NM 7", "BGS 7"),
                            ("Kobe Bryant Rookie - PSA EX 5, PSA/DNA NM-MT 8", "PSA 5"), ("2003 Topps Chrome LeBron BGS GEM MINT 9.5", "BGS 9.5"),
                            ("1933 Goudey Ruth PSA EX-MT+ 6.5", "PSA 6.5"), ("Flagg Debut Patch 1/1 #DPA-CF PSA 10", "PSA 10")]:
            self.assertEqual(H.parse_title(title)["grade"], want, title)
        for title in ("2025 Topps Chrome Superfractor LeBron James 1/1 #127 CGC AUTH", "PMG Red #23 Jordan (#063/100) - PSA Authentic/Altered"):
            self.assertIsNone(H.parse_title(title)["grade"], title)

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


class CardMatches(unittest.TestCase):
    """Real rows from the first headline review (2026-09-29): wrong ones must fail, right ones must pass."""
    def check(self, title, set_name, parallel, want, print_run=None, cand_run=None):
        ok, why = H.card_matches(title, print_run, set_name, parallel, cand_run)
        self.assertEqual(ok, want, "%s | %s | %s -> %s" % (title, set_name, parallel, why))

    def test_wrong_matches_fail(self):
        self.check("2023-24 Panini Donruss FIFA Kaboom! Black #7 Lamine Yamal Rookie Card (#1/1)", "2024 Panini Select FIFA", "Black", False)
        self.check("2019-20 Topps Chrome Bundesliga Autographs SuperFractor #72 Erling Haaland Signed Rookie Card (#1/1)",
                   "2019 Topps Chrome Bundesliga", "", False)
        self.check("2021 Absolute Kaboom! Green Tom Brady 1/1 #K24 PSA 8 NM-MT", "2021 Panini Absolute Kaboom", "", False)
        self.check("2018 Bowman Chrome Superfractor #1 Shohei Ohtani 1/1 BGS 9.5", "2019 Topps Archives", "Superfractor", False)
        self.check("1997-98 SkyBox Metal Universe Precious Metal Gems Green #81 Kobe Bryant /10 PSA 5", "1997 Metal Universe",
                   "Precious Metal Gems Red", False)
        self.check("2018 Bowman Chrome Orange Refractor Shohei Ohtani ROOKIE /25 #BCRASO", "2018 Bowman Chrome Rookie Autographs",
                   "Refractor", False)                         # plain Refractor is not the Orange /25

    def test_right_matches_pass(self):
        self.check("2018 Bowman Chrome Orange Refractor Shohei Ohtani ROOKIE /25 #BCRASO MBA BGS 9.5 GEM AUTO 10",
                   "2018 Bowman Chrome Rookie Autographs", "Orange Refractor", True)
        self.check("2023-24 Panini Prizm Nebula Choice Prizm #136 Victor Wembanyama Rookie Card (#1/1)", "2023 Panini Prizm",
                   "Choice Nebula", True)
        self.check("2012-13 Panini Prizm Gold Prizm #1 LeBron James (#07/10) - BGS GEM MINT 9.5", "2012 Panini Prizm", "Gold Prizm",
                   True, print_run=10, cand_run=10)
        self.check("1956 Topps Mickey Mantle (Gray Back) #135 PSA Mint 9", "1956 Topps", "Gray Back", True)
        self.check("1952 Topps #311 Mickey Mantle - SGC NM+ 7.5", "1952 Topps", "", True)
        self.check("1997-98 SkyBox Metal Universe Precious Metal Gems (PMG) Red #23 Michael Jordan (#039/100) - BGS",
                   "1997 Metal Universe", "Precious Metal Gems Red", True)
        self.check("2025 Topps Chrome Superfractor LeBron James 1/1 #127 CGC AUTH", "2025 Topps Chrome", "Superfractor", True)

    def test_team_and_grade_words_are_not_parallels(self):
        self.check("2002 Bowman Chrome #101 David Ortiz Boston Red Sox PSA 10", "2002 Bowman Chrome", "", True)
        self.check("2003-04 Topps Chrome #111 LeBron James BGS 10 Pristine Black Label", "2003 Topps Chrome", "", True)

    def test_year_must_agree(self):
        ok, why = H.card_matches("2025 Topps Chrome SuperFractor #1 Shohei Ohtani (#1/1) - PSA GEM MT 10", None, "2024 Topps Chrome",
                                 "Superfractor", None, 2025, 2024)
        self.assertFalse(ok, why)
        ok, why = H.card_matches("2023-24 Panini Prizm #136 Victor Wembanyama", None, "2023 Panini Prizm", "", None, 2023, 2023)
        self.assertTrue(ok, why)

    def test_parallel_modifiers(self):
        self.check("2014 Panini Prizm World Cup Gold Power Prizm #12 Lionel Messi (#2/5) - BGS GEM", "2014 Panini Prizm World Cup",
                   "Gold Prizm", False)
        self.check("1909-11 T206 White Border Honus Wagner Sweet Caporal PSA 1", "1909 T206", "", True)

    def test_product_lines_both_ways(self):
        self.check("1975 Topps Mini #228 George Brett Rookie Card - PSA GEM MT 10", "1975 Topps", "", False)
        self.check("1996-97 Topps Chrome #138 Kobe Bryant Rookie Card - PSA GEM MT 10", "1996 Topps", "", False)
        self.check("2025 Topps Chrome Update Cooper Flagg #1 PSA 10", "2025 Topps Chrome", "", False)
        self.check("2023 Donruss Optic Gold Power Lionel Messi 1/1 #1 BGS 7 NRMT", "2023 Panini Donruss", "Gold Power Optic", True)
        self.check("2018-19 Panini Prizm Mosaic Black Prizm #68 Luka Doncic Rookie Card (#1/1)", "2018 Panini Prizm Mosaic", "Black", True)
        self.check("1986 Fleer Sticker Michael Jordan ROOKIE #8 PSA 10 GEM MINT", "1986 Fleer Sticker", "", True)
        self.check("1986 Fleer #57 Michael Jordan Rookie PSA 8", "1986 Fleer Sticker", "", False)
        self.check("1997-98 Fleer Ultra Masterpiece #23P Michael Jordan 1/1 PSA 8", "1997 Ultra", "Masterpiece", True)

    def test_image_variation_is_its_own_card(self):
        self.check("2018 Topps Chrome Variation Orange Refractor Shohei Ohtani ROOKIE /25 #150 BGS 10", "2018 Topps Chrome", "Orange Refractor", False)

    def test_sets_and_lots_are_not_single_cards(self):
        self.check("1986 Fleer Basketball Complete Set w/ Michael Jordan ROOKIE #57 PSA 8", "1986 Fleer", "", False)
        self.check("Lot (20) 2013 Complete Set Panini Innovation Kaboom #1-#20 PSA", "2013 Panini Innovation", "Kaboom", False)
        self.check("1986 Fleer Basketball Unopened Wax Box #57", "1986 Fleer", "", False)
        self.check("2003 Exquisite Collection Limited Logos LeBron James ROOKIE PATCH AUTO /75 #LL-LJ", "2003 Exquisite Collection Limited Logos",
                   "Patch Auto", True)
        self.check("1952 Topps #311 Mickey Mantle (PSA 8)", "1952 Topps", "", True)

    def test_after_market_signatures(self):
        self.check("1986 Fleer Basketball Michael Jordan ROOKIE AUTO #57 BAS BGS 7 NRMT", "1986 Fleer", "", False)
        self.check("1986-87 Fleer #57 Michael Jordan Signed Rookie Card - PSA EX 5, PSA/DNA NM-MT 8", "1986 Fleer", "", False)
        self.check("2018 Bowman Chrome Shohei Ohtani ROOKIE AUTO DNA 10 #BCRASO MBA PSA 10 GEM MINT",
                   "2018 Bowman Chrome Rookie Autographs", "", True)
        self.check("2017 Panini Contenders Red Zone Patrick Mahomes II ROOKIE AUTO DNA 10 #303 PSA 10", "2017 Panini Contenders",
                   "Autograph Red Zone", True)

    def test_print_run_must_agree(self):
        self.check("2012-13 Panini Prizm Gold Prizm #1 LeBron James /10", "2012 Panini Prizm", "Gold Prizm", False, print_run=10, cand_run=25)


if __name__ == "__main__":
    unittest.main(verbosity=1)
