#!/usr/bin/env python3
"""Offline tests for the pure parts of tools/ebay_veefriends_pilot.py (no browser, no eBay contact)."""
import os, sys
sys.path.insert(0, os.path.expanduser("~/whatnot-sniper/tools")); sys.path.insert(0, os.path.expanduser("~/whatnot-sniper"))
import ebay_veefriends_pilot as P
from ebay_player_scraper import parse_listing_sold_date
PASS = FAIL = 0
def check(n, got, want):
    global PASS, FAIL
    ok = got == want; PASS += ok; FAIL += (not ok); print(f"  {'PASS' if ok else 'FAIL'}  {n}" + ("" if ok else f"  got={got!r} want={want!r}"))
u = P.sold_url("veefriends", 3, "183050")
check("category path", u.startswith("https://www.ebay.com/sch/183050/i.html?"), True)
check("sold URL has sold+completed, newest-first, 240/page, page 3", all(x in u for x in ("LH_Sold=1", "LH_Complete=1", "_sop=13", "_ipg=240", "_pgn=3")), True)
check("item id from /itm/<slug>/<id>", P.item_id("https://www.ebay.com/itm/veefriends-skeleton/187654321098?hash=x"), "187654321098")
check("item id from /itm/<id>", P.item_id("https://www.ebay.com/itm/387654321098"), "387654321098")
items = [{"title": "2026 Topps Chrome VeeFriends Skilled Skeleton #159", "priceText": "$12.50", "soldDate": "Sold  Sep 20, 2026", "link": "https://www.ebay.com/itm/111111111111", "bestOffer": False},
         {"title": "Pokemon Charizard", "priceText": "$99", "soldDate": "Sold  Sep 20, 2026", "link": "https://www.ebay.com/itm/222222222222"},
         {"title": "VeeFriends sticker Lava", "priceText": "$5.00", "soldDate": "Sold  Jun 28, 2026", "link": "https://www.ebay.com/itm/333333333333", "bestOffer": True}]
rows, oldest = P.page_rows(items, "q", 1, parse_listing_sold_date, lambda t: float(str(t).replace("$", "")))
check("non-VeeFriends rows dropped", [r["item_id"] for r in rows], ["111111111111", "333333333333"])
check("oldest date seen drives the floor stop", oldest, "2026-06-28")
check("best offer flag carried", rows[1]["best_offer"], True)
print(f"\nRESULT: {PASS} passed, {FAIL} failed"); sys.exit(1 if FAIL else 0)
