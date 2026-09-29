#!/usr/bin/env python3
"""Unit tests for rea_scraper pure core. Fixture is REAL markup captured from
collectrea.com/archives on 2026-07-16. Stdlib-only."""
import sys
from rea_scraper import deslug, season_to_date, parse_rea_listing

fails = total = 0
def check(c, label, d=""):
    global fails, total
    total += 1
    print("  [%s] %s%s" % ("PASS" if c else "FAIL", label, ("  " + d if d and not c else "")))
    fails += (not c)

print("== deslug ==")
check("Babe Ruth" in deslug("1914-baltimore-news-babe-ruth-rookie"), "de-hyphenates + capitalizes")
check("1914" in deslug("1914-baltimore-news-babe-ruth"), "keeps year token")
check(deslug("") == "", "empty -> empty")

print("== season_to_date ==")
check(season_to_date("2023", "Fall") == "2023-11-01", "fall")
check(season_to_date("2024", "spring") == "2024-05-01", "spring lowercase")
check(season_to_date("2025", "Marketplace") == "2025-07-01", "marketplace -> mid-year")
check(season_to_date("2026", "Winter") == "2026-01-15", "winter")
check(season_to_date("2022", "Unknownseason") == "2022-07-01", "unknown -> mid-year")
check(season_to_date("bad", "Fall") == "", "bad year -> empty")

print("== parse_rea_listing (real markup) ==")
FIX = '''
<div><a href="/archives/2023/Fall/1/extremely-rare-1914-baltimore-news-babe-ruth-rookie-sgc-vg-3-babe-ruth-museum-provenance" class="flex flex-col">
Read more Prewar Baseball Extremely Rare 1914 Baltimore News Babe Ruth Rookie SGC VG 3 Lot 1 - $7,200,000 </a></div>
<div><a href="/archives/2021/Summer/1/1909-1911-t206-white-border-honus-wagner-sgc-vg-3" class="flex">
T206 Honus Wagner Lot 1 - $6,606,296 </a></div>
<div><a href="/archives/2025/Marketplace/1303/1980-topps-larry-bird-magic-johnson-rookie-psa-10">
Bird Magic RC Lot 1303 - $1,528,066 </a></div>
<div><a href="/nav/other">nav link no price</a></div>
'''
lots = parse_rea_listing(FIX)
check(len(lots) == 3, "3 lots parsed (nav link w/o price dropped)", "got %d" % len(lots))
l0 = lots[0]
check(abs(l0["sold_price"] - 7200000) < 1, "wagner ruth price 7.2M", str(l0["sold_price"]))
check(l0["year"] == "2023" and l0["season"] == "Fall" and l0["lot"] == "1", "year/season/lot from url")
check(l0["sold_date"] == "2023-11-01", "sold_date from season")
check("Babe Ruth" in l0["title"], "title from slug")
check(l0["url"] == "https://collectrea.com/archives/2023/Fall/1/extremely-rare-1914-baltimore-news-babe-ruth-rookie-sgc-vg-3-babe-ruth-museum-provenance", "absolute url")
check(abs(lots[2]["sold_price"] - 1528066) < 1, "marketplace lot price", str(lots[2]["sold_price"]))
check(lots[2]["season"] == "Marketplace", "marketplace season parsed")
# price association: each lot gets ITS OWN price, not the next lot's
check(abs(lots[1]["sold_price"] - 6606296) < 1, "2nd lot price = its own (6.6M)", str(lots[1]["sold_price"]))

print("\n%d/%d passed, %d failed" % (total - fails, total, fails))
sys.exit(1 if fails else 0)
