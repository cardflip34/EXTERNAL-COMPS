#!/usr/bin/env python3
"""Unit tests for goldin_scraper_v2 pure core. Fixtures are REAL records
captured from goldin.co's /api/auctions and /api/lots_v2 on 2026-07-04
(trimmed to relevant fields). Prints PASS/FAIL per case; exits nonzero on fail.
Run: python3 test_goldin_v2.py  (works locally and on Mini — no playwright import)
"""
import sys

from goldin_scraper_v2 import (
    pick_newest_completed, is_sold, is_excluded_title, lot_to_comp,
    merge_auction_lists, extract_lots,
)

# ── real fixtures (captured 2026-07-04) ──────────────────────────────────────
AUCTIONS = [
    {"title": "2012 - November Catalog Auction", "auction_type": "Weekly",
     "status": "Completed", "auction_id": "A-2012NOV",
     "end_timestamp": "2012-11-18T03:16:00Z", "start_timestamp": "2012-10-15T08:30:00Z"},
    {"title": "2026 Weekly Auction Jun 23 - Jul 2", "auction_type": "Weekly",
     "status": "Completed", "auction_id": "A-2026JUL2",
     "end_timestamp": "2026-07-03T02:00:00Z", "start_timestamp": "2026-06-23T08:30:00Z"},
    {"title": "TCG Weekly Auction Jun 17 - Jun 28", "auction_type": "Weekly",
     "status": "Completed", "auction_id": "A-2026JUN28",
     "end_timestamp": "2026-06-29T02:00:00Z", "start_timestamp": "2026-06-17T08:30:00Z"},
    {"title": "2026 July Elite", "auction_type": "Premier",
     "status": "Active", "auction_id": "A-2026ELITE",
     "end_timestamp": "2026-07-12T02:00:00Z", "start_timestamp": "2026-06-20T08:30:00Z"},
    {"title": "broken row (no end)", "status": "Completed", "auction_id": "A-NOEND"},
]

LOT_SOLD = {  # real 2012 lot, trimmed
    "auction_id": "A-2012NOV", "auction_type": "Weekly", "buyer_premium": 20.0,
    "current_price": 21438.0, "end_timestamp": "2012-11-18T03:16:00Z",
    "lot_id": "L-RUTHBAT", "lot_number": 7,
    "meta_slug": "exceptional-1930s-babe-ruth-signed-bat-psa-dnaibzy1",
    "number_of_bids": 1.0, "primary_image_name": "694a_lg",
    "status": "Completed_Sold", "title": "Exceptional 1930s Babe Ruth Signed Bat PSA/DNA",
}
LOT_UNSOLD = dict(LOT_SOLD, lot_id="L-UNSOLD", status="Completed_Unsold")
LOT_ACTIVE = dict(LOT_SOLD, lot_id="L-ACTIVE", status="Active")
LOT_ZERO = dict(LOT_SOLD, lot_id="L-ZERO", current_price=0.0)
LOT_NOPRICE = dict(LOT_SOLD, lot_id="L-NOPRICE", current_price=None)
LOT_CARD = dict(LOT_SOLD, lot_id="L-CARD", current_price=14153.0,
                end_timestamp="2026-07-03T02:00:00Z", auction_id="A-2026JUL2",
                meta_slug="2019-panini-prizm-zion-williamson-silver-psa-10-xyz",
                title="2019 Panini Prizm Zion Williamson Silver PSA 10")

fails = 0
total = 0

def check(cond, label, detail=""):
    global fails, total
    total += 1
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", label,
                           ("  " + detail if detail and not cond else "")))
    fails += (not cond)


print("== pick_newest_completed ==")
got = pick_newest_completed(AUCTIONS, 2, set())
check([a["auction_id"] for a in got] == ["A-2026JUL2", "A-2026JUN28"],
      "newest-2 completed, desc order", repr([a["auction_id"] for a in got]))
got = pick_newest_completed(AUCTIONS, 5, set())
check(all(a["status"] == "Completed" for a in got), "Active auction filtered out")
check(all(a["auction_id"] != "A-NOEND" for a in got), "row missing end_timestamp skipped")
got = pick_newest_completed(AUCTIONS, 3, {"A-2026JUL2"})
check([a["auction_id"] for a in got][:1] == ["A-2026JUN28"],
      "already-done auction skipped (recency cursor)", repr([a["auction_id"] for a in got]))
check(pick_newest_completed([], 3, set()) == [], "empty input -> empty")
check(pick_newest_completed(AUCTIONS, 0, set()) == [], "n=0 -> empty")

print("== is_sold ==")
check(is_sold(LOT_SOLD) is True, "Completed_Sold -> True")
check(is_sold(LOT_UNSOLD) is False, "Completed_Unsold -> False")
check(is_sold(LOT_ACTIVE) is False, "Active -> False")
check(is_sold({}) is False, "empty -> False")
check(is_sold(None) is False, "None -> False")

print("== is_excluded_title ==")
check(is_excluded_title("Lot of 5 Prizm Rookies") is True, "'Lot of 5' excluded")
check(is_excluded_title("2020 Bowman Chrome Sealed Hobby Box") is True, "sealed box excluded")
check(is_excluded_title("Complete Set of 1987 Topps") is True, "complete set excluded")
check(is_excluded_title("2019 Panini Prizm Zion Williamson Silver PSA 10") is False,
      "normal single kept")
check(is_excluded_title("Charlotte Hornets Michael Jordan Card PSA 9") is False,
      "'lot' inside 'Charlotte' must NOT match (word boundary)")
check(is_excluded_title("Booster pull Pikachu VMAX") is True, "booster excluded")
check(is_excluded_title("") is False, "empty title kept (later filters decide)")

print("== lot_to_comp ==")
auction = AUCTIONS[1]
row = lot_to_comp(LOT_CARD, auction, 42)
check(row is not None, "sold card maps to a row")
check(row["comp_id"] == "GDV2-42", "comp id numbering")
check(row["sold_price"] == 14153.0, "price mapped")
check(row["sold_date"] == "2026-07-03", "sold_date = lot end_timestamp date part",
      repr(row["sold_date"]))
check(row["url"] == "https://goldin.co/item/2019-panini-prizm-zion-williamson-silver-psa-10-xyz",
      "url built from meta_slug")
check(row["lot_id"] == "L-CARD" and row["auction_id"] == "A-2026JUL2",
      "ids carried for dedup/provenance")
check(row["auction_title"] == auction["title"], "auction title attached")
check(lot_to_comp(LOT_UNSOLD, auction, 1) is None, "unsold -> None")
check(lot_to_comp(LOT_ZERO, auction, 1) is None, "zero price -> None")
check(lot_to_comp(LOT_NOPRICE, auction, 1) is None, "null price -> None")
no_end = dict(LOT_CARD, lot_id="L-NOEND2", end_timestamp="")
row2 = lot_to_comp(no_end, auction, 1)
check(row2 is not None and row2["sold_date"] == "2026-07-03",
      "missing lot end falls back to auction end date")
row3 = lot_to_comp(dict(no_end, lot_id="L-NOEND3"), {"auction_id": "X"}, 1)
check(row3 is None, "no date anywhere -> None (never land undated comps)")

print("== merge_auction_lists / extract_lots ==")
merged = merge_auction_lists([{"auctions": AUCTIONS[:2]}, {"auctions": AUCTIONS[1:3]}])
check(len(merged) == 3, "merge dedupes by auction_id", repr(len(merged)))
LOT_UNSOLD_JUL = dict(LOT_UNSOLD, auction_id="A-2026JUL2", lot_id="L-UNSOLD-JUL")
payloads = [
    {"searchalgolia": {"lots": [LOT_SOLD, LOT_CARD], "total": 794277}},
    {"searchalgolia": {"lots": [LOT_CARD, LOT_UNSOLD_JUL], "total": 2}},
]
lots, tot = extract_lots(payloads, "A-2026JUL2")
check(len(lots) == 2 and {l["lot_id"] for l in lots} == {"L-CARD", "L-UNSOLD-JUL"},
      "filters to target auction + dedupes by lot_id", repr([l["lot_id"] for l in lots]))
check(all(l["auction_id"] == "A-2026JUL2" for l in lots),
      "cross-auction lot (L-RUTHBAT, 2012) correctly dropped")
check(tot == 2, "total taken from a payload containing target-auction lots", repr(tot))
lots0, tot0 = extract_lots(payloads, "A-NOTHERE")
check(lots0 == [] and tot0 is None, "unknown auction -> empty")

print("\n%d/%d passed, %d failed" % (total - fails, total, fails))
sys.exit(1 if fails else 0)
