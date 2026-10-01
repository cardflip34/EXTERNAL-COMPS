#!/usr/bin/env python3
"""Unit tests for rea_scraper pure core. Fixture is REAL markup captured from
collectrea.com/archives on 2026-07-16. Stdlib-only."""
import sys
import rea_scraper
from rea_scraper import deslug, season_to_date, parse_rea_listing, is_single_card, lots_of_auction, parse_years

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

print("== is_single_card ==")
for title, want in [
    ("1948 1949 Leaf 79 Jackie Robinson Rookie", True),
    ("1909 1911 T206 White Border Honus Wagner SGC VG 3", True),
    ("T206 Honus Wagner", True),
    ("1952 Topps 311 Mickey Mantle PSA NM MT 8", True),
    ("Cap Anson 1888 Goodwin Champions", True),
    ("1948 To 1951 Bowman Scrapbook Collection (302) With 27 Hall Of Famers", False),
    ("Dick Perez The Immortals Original Artwork Collection The Negro Leagues 29", False),
    ("1952 Topps Complete Set (407)", False),
    ("1927 Babe Ruth Game Used Bat PSA DNA", False),
    ("1960s Mickey Mantle Signed Baseball", False),
    ("1986 Fleer Basketball Wax Box", False),
    ("Babe Ruth Signed Letter", False),
]:
    check(is_single_card(title) == want, "%s -> %s" % (title[:50], want))
check(is_single_card("1960s Mickey Mantle Signed Ball") is False, "signed ball is memorabilia")
for title in ("1941 Play Ball 8 Mel Ott Sgc Nm 7", "T206 Fred Clarke Holding Bat Psa Nm 7", "1909 1911 T206 Joe Tinker Bat Off Shoulder Psa Vg 3",
              "1909 1911 T206 Vic Willis With Bat Psa Vg Ex 4", "1925 W590 Babe Ruth King Of The Bat Psa Authentic",
              "2000 Playoff Contenders Rookie Ticket Autograph 144 Tom Brady Bgs 8"):
    check(is_single_card(title), "card kept: " + title[:44])
for title in ("1927 Babe Ruth Game Used Bat PSA DNA", "Yankees Team Signed Baseball Bat", "1932 World Series Ticket Stub Babe Ruth Called Shot"):
    check(not is_single_card(title), "memorabilia skipped: " + title[:44])

print("== lots_of_auction / parse_years ==")
fake = [{"year": "2025", "season": "Spring"}, {"year": "2024", "season": "Spring"}, {"year": "2025", "season": "Fall"}]
check(lots_of_auction(fake, 2025, "spring") == [fake[0]], "only the requested auction's lots")
check(parse_years('<select name="soldYear"><option value="">All</option><option value="2026">2026</option>'
                  '<option value="1999">1999</option></select><select><option value="2030">x</option></select>') == [2026, 1999],
      "years from the soldYear select only")

print("== price outside the link segment (1 lot per page was dropped) ==")
SPLIT = '''<a href="/archives/2025/Spring/7/lot-seven">x</a><a href="/archives/2025/Spring/8/1915-e145-cracker-jack-103-joe-jackson-psa-ex-5">img</a>
<a href="/archives/2025/Spring/9/lot-nine">y</a><div>Lot 7 - $1,000</div><div>Lot 8 - $99,000</div><div>Lot 9 - $5,000</div>'''
sp = {l["lot"]: l["sold_price"] for l in parse_rea_listing(SPLIT)}
check(sp == {"7": 1000.0, "8": 99000.0, "9": 5000.0}, "each lot gets ITS Lot-N price even when the text sits elsewhere", str(sp))
MIXED = '''<a href="/archives/2023/Fall/1/a">x</a> Lot 1 - $7,200,000 <a href="/archives/2021/Summer/1/b">y</a> Lot 1 - $6,606,296'''
check([l["sold_price"] for l in parse_rea_listing(MIXED)] == [7200000.0, 6606296.0], "repeated lot numbers fall back to the segment")

print("== memorabilia with a year and a grader is not a card ==")
for title in ("High Grade 1953 Walt Alston Single-Signed Baseball (PSA)", "1965-67 Mickey Mantle New York Yankees Game Used Bat - Newly Discovered! (PSA GU 9.5)",
              "Exceptional Photomatched 1922 1924 Babe Ruth New York Yankees Game Used Bat Psadna Gu 10", "1938 Lou Gehrig New York Yankees Game Used Road Jersey",
              "1934 New York Yankees Team Signed Baseball 24 Signatures Including Babe Ruth And Lou Gehrig",
              "1984 Michael Jordan Chicago Bulls Signed Game Worn Nike Air Ship Rookie Sneakers"):
    check(not is_single_card(title), "memorabilia skipped: " + title[:46])
for title in ("1947 1966 Exhibits Gil Hodges Signed B On Cap Variation Psadna Auto 9", "1952 Topps #311 Mickey Mantle Signed Card PSA/DNA 8",
              "2003-04 Upper Deck Exquisite Collection Rookie Patch Autograph 78 LeBron James BGS 9.5",
              "2018 Topps Chrome Game Used Relic Patch Card Shohei Ohtani #GUR-SO PSA 10"):
    check(is_single_card(title), "card kept: " + title[:46])

print("== Huggins & Scott (same platform) ==")
HS = '''<div><a href="/auction/2025/Fall/1/1961-topps-dice-game-willie-mays-psa-ex-5" class="flex">
Willie Mays Lot 1 - $156,000 </a></div>
<div><a href="/auction/2007/March/1/incredible-christy-mathewson-single-signed-heydler-baseball">Lot 1 - $270,600</a></div>'''
hs = parse_rea_listing(HS, "https://hugginsandscott.com", "auction")
check(len(hs) == 2 and hs[0]["url"] == "https://hugginsandscott.com/auction/2025/Fall/1/1961-topps-dice-game-willie-mays-psa-ex-5",
      "H&S lot links parsed with the H&S host and /auction/ prefix")
check(abs(hs[0]["sold_price"] - 156000) < 1 and hs[1]["season"] == "March", "H&S price + month-named auction")
check(parse_rea_listing(HS) == [], "REA defaults do not read H&S links")
check(season_to_date("2007", "March") == "2007-03-15" and season_to_date("2022", "november") == "2022-11-15", "month auctions")
check(not is_single_card(hs[1]["title"]), "a signed baseball is not a single card")
rea_scraper.use_house("hugginsandscott")
check(rea_scraper.BASE == "https://hugginsandscott.com/auction" and rea_scraper.OUT_FILE.endswith("hugginsandscott_comps.json")
      and rea_scraper.HOUSE["id"] == "HS", "use_house switches base, files and id")
rea_scraper.use_house("rea")
check(rea_scraper.BASE == "https://collectrea.com/archives" and rea_scraper.OUT_FILE.endswith("rea_comps.json"), "and back")

print("\n%d/%d passed, %d failed" % (total - fails, total, fails))
sys.exit(1 if fails else 0)
