#!/usr/bin/env python3
"""Tests for the 2026-09-29 Goldin changes: the single-card title test for untagged (pre-~2023) auctions, the
non-card auction skip, and the bridge guard against lots already in Neon under an older 'GD-' id. No network, no DB.

  python3 test_goldin_untagged.py
"""
import os, sys, unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import goldin_scraper_v2 as G  # noqa: E402
import bridge_local_sources_to_neon as B  # noqa: E402

SINGLES = [  # real titles from untagged Goldin auctions (2017, 2021) plus common shapes
    "2003-04 Upper Deck SP Authentic Autograph #148 LeBron James Signed Rookie Card (#293/500)",
    "2003-04 Topps Chrome Refractors #111 LeBron James Rookie Card – BGS GEM MINT 9.5",
    '2000 Playoff Contenders "Rookie Ticket" Autograph #144 Tom Brady Signed Rookie Card - BGS NM-MT 8',
    "1887 N28 Allen & Ginter “The World's Champions” Baseball Players Cap Anson - PSA 5",
    "1906 Fan Craze A.L. Cy Young – PSA MINT 9",
    "1999 Pokemon Base Set 1st Edition Holo Charizard #4 PSA GEM MT 10",
    "1952 Topps #311 Mickey Mantle Autographed Baseball Card - PSA/DNA",
    "2019 Topps Chrome Printing Plate 1/1 Vladimir Guerrero Jr. Rookie",
    # real titles the first filter skipped (2013 April auction spot check): T206 poses and checklist cards
    "T206 Fred Clarke (Holding Bat) PSA NM 7",
    "T206 Joe Tinker Bat Off Shoulder (Old Mill) PSA VG 3",
    "T206 Vic Willis With Bat (Polar Bear) PSA 4 VG-EX",
    "1911 T3 Turkey Red Addie Joss PSA Authentic (Checklist Back)",
    "1957 Topps Checklist 1/2 (Big Blony) PSA NM-MT 8",
    "1887 N284 Gold Coin Charles Comiskey PSA FR 1.5",
    "1941 Play Ball #8 Mel Ott SGC NM 7",
    "1925 W590 Babe Ruth King of the Bat PSA Authentic",
]
NOT_SINGLES = [
    '1959 Fleer "Three Stooges" PSA MINT 9 Collection (38 Different)',
    "Circa 1909 Rube Waddell Ultra Rare Signed Cabinet Card - PSA/DNA Authentic",
    "1963 Amazing Spider-Man #1 CGC 9.2 White Pages",
    "1985 Nintendo Super Mario Bros. WATA 9.4 A+ Sealed",
    "Michael Jordan 1998 Game-Worn Chicago Bulls Jersey",
    "1927 Babe Ruth Signed Baseball PSA/DNA",
    "2020 Panini Prizm Justin Herbert Rookie Card Lot (5)",
    "1986 Fleer Basketball Unopened Wax Box",
    "Tom Brady Signed Helmet",
    # real titles the first filter kept, and memorabilia around the new bat/ball exceptions
    "Impressive 1962 Safe at Home Lobby Card Signed by Mantle, Maris and Tresh.",
    "1995 World Champion Atlanta Braves Signed Billy Lopa Giclee on Canvas (21 Signatures)  #41/50",
    "Yankee Legends Signed Baseball Bat with 12 Signatures (PSA 9)",
    "1927 Babe Ruth Louisville Slugger Game Bat PSA/DNA",
    "Lot of Ten  (10) Ted Williams Signed 16x20 Photos  PSA/DNA and Williams Hologram (HOF)",
    "1949 Bowman Baseball Collection PSA NM 7 (18 diff.)",
    "1926 W512 Baseball Uncut Strip with Babe Ruth PSA Authentic",
    "Ty Cobb Signed Check PSA MINT 9",
    "1893 $20 Gold Coin PCGS MS 62",
]


class UntaggedTitleTest(unittest.TestCase):
    def test_singles_kept(self):
        for t in SINGLES:
            self.assertTrue(G.is_untagged_single_card(t), t)

    def test_non_singles_skipped(self):
        for t in NOT_SINGLES:
            self.assertFalse(G.is_untagged_single_card(t), t)

    def test_non_card_auctions(self):
        for t in ("2021 November Comics & Video Games", "2026 Spring Coin Auction", "2026 Summer Game Used Memorabilia Auction",
                  "2026 Winter Hollywood Memorabilia Auction", "2026 Photography Auction"):
            self.assertTrue(G.is_non_card_auction(t), t)
        for t in ("2021 November Monthly Auction", "2022 Winter Goldin Elite Session 1", "2017 - Great American Trading Card Auction Ending April 1"):
            self.assertFalse(G.is_non_card_auction(t), t)


class TaggedPathUnchanged(unittest.TestCase):
    def test_v1_blocklist_still_drops_checklists_on_tagged_auctions(self):
        self.assertTrue(G.is_excluded_title("1957 Topps Checklist 1/2 (Big Blony) PSA NM-MT 8"))


class GdDuplicateGuard(unittest.TestCase):
    def test_drops_only_matches_of_an_older_gd_row(self):
        gd = [("Logan Paul's Pikachu Illustrator PSA 10", 16492000, date(2026, 2, 15)),
              ("1916 M101-5 Babe Ruth", 1415200, date(2026, 3, 7))]
        rows = [
            {"title": "Logan Paul's Pikachu Illustrator PSA 10", "sold_price": 16492000.0, "sold_date": "2026-02-16"},  # +1 day: dup
            {"title": "1916 M101-5 Babe Ruth", "sold_price": 1415200, "sold_date": "2026-03-10"},                      # 3 days: keep
            {"title": "1916 M101-5 Babe Ruth", "sold_price": 1415300, "sold_date": "2026-03-07"},                      # price differs
            {"title": "Something else", "sold_price": 100, "sold_date": "2026-03-07"},
        ]
        kept = B.drop_goldin_gd_duplicates(rows, gd)
        self.assertEqual([r["sold_date"] for r in kept], ["2026-03-10", "2026-03-07", "2026-03-07"])

    def test_string_dates_from_the_db_driver(self):
        kept = B.drop_goldin_gd_duplicates([{"title": "A", "sold_price": 5, "sold_date": "2026-01-02"}], [("A", "5.00", "2026-01-01")])
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
