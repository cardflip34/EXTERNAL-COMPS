#!/usr/bin/env python3
"""
goldin_scraper_v2.py — Goldin recency-first sold-lots capture (auction-scoped)
===============================================================================
v2 rationale (2026-07-04): v1 walked the ENTIRE sold catalog sorted by
Highest_Bids (no recency) and stored sold_date="" (the grid shows no dates).
v2 scopes capture to the N most recently CLOSED auctions and reads the site's
own JSON backend instead of DOM text:

  - /api/auctions        -> full auction calendar (status, end_timestamp)
  - /api/lots_v2         -> per-lot records: title, current_price (realized),
                            status Completed_Sold/..., lot_id, end_timestamp

Both are captured by response-interception while Playwright drives the normal
buy pages (goldin.co tar-pits plain HTTP, but renders fine in a real browser —
verified from Mini 2026-07-04, headless OK). sold_date = auction end date.

Recency cursor: auctions already fully captured are recorded in
goldin_v2_state.json and skipped, so each run lands only new closed auctions.

Output: goldin_comps_v2.json (NEW file — v1 goldin_comps.json is untouched).
Local JSON only; any Neon bridge is a separate operator-approved step.

Usage:
  python3 goldin_scraper_v2.py --dry-run                # list target auctions only
  python3 goldin_scraper_v2.py --newest-auctions 3      # capture 3 newest closed
  python3 goldin_scraper_v2.py --auction-id <id>        # one specific auction
  python3 goldin_scraper_v2.py --newest-auctions 1 --max-pages-per-auction 3
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
V2_FILE = os.path.join(_SCRIPT_DIR, "goldin_comps_v2.json")
V2_STATE = os.path.join(_SCRIPT_DIR, "goldin_v2_state.json")

BUY_URL = "https://goldin.co/buy/?show_only=Sold%20Items&lot_view=grid&number_of_lots=24"
AUCTION_PAGE_URL = ("https://goldin.co/buy/?show_only=Sold%20Items"
                    "{item_type}"
                    "&Auctions={auction_id}"
                    "&page={page}"
                    "&number_of_lots=240"
                    "&lot_view=grid")
SINGLES_PARAM = "&Item_Type=Single%20Cards"
ITEM_URL_PREFIX = "https://goldin.co/item/"

# Same non-singles guard as v1 (belt-and-suspenders on top of Item_Type param).
TITLE_EXCLUSIONS = {
    "lot", "lots", "bundle", "collection", "pack", "packs", "box", "boxes",
    "sealed", "mystery", "repack", "supplies", "digital", "wrapper", "checklist",
    "multiple", "set of", "complete set", "bulk", "case", "hobby box", "blaster",
    "hanger", "cello", "fat pack", "mega box", "retail box", "booster",
}

SOLD_STATUSES = {"Completed_Sold"}


# ── pure core (unit-tested in test_goldin_v2.py) ─────────────────────────────
def pick_newest_completed(auctions, n, already_done):
    """From /api/auctions records, return the n most recently ENDED auctions
    with status 'Completed' whose auction_id is not in already_done.
    Sorted newest end_timestamp first. Records missing id/end are skipped."""
    done = set(already_done or ())
    cands = []
    for a in auctions or []:
        aid = a.get("auction_id")
        end = a.get("end_timestamp") or ""
        if not aid or not end:
            continue
        if a.get("status") != "Completed":
            continue
        if aid in done:
            continue
        cands.append(a)
    cands.sort(key=lambda a: a.get("end_timestamp") or "", reverse=True)
    return cands[:max(0, n)]


def pick_auctions_in_window(auctions, start_iso, end_iso, already_done):
    """All Completed auctions whose end_timestamp is in [start_iso, end_iso),
    OLDEST-first (chronological backfill), skipping already_done. end_timestamp is
    ISO-8601 (e.g. '2025-07-03T02:00:00Z') so lexical compare against 'YYYY-MM-DD'
    bounds is correct."""
    done = set(already_done or ())
    cands = []
    for a in auctions or []:
        aid = a.get("auction_id")
        end = a.get("end_timestamp") or ""
        if not aid or not end or a.get("status") != "Completed" or aid in done:
            continue
        if start_iso <= end < end_iso:
            cands.append(a)
    cands.sort(key=lambda a: a.get("end_timestamp") or "")
    return cands


def is_sold(lot):
    """True only for lots Goldin marks as actually SOLD."""
    return (lot or {}).get("status") in SOLD_STATUSES


def is_excluded_title(title):
    """v1-compatible non-singles guard: word/phrase blocklist, case-insensitive.
    Multi-word phrases match as substrings; single words match on word
    boundaries ('lot' must not kill 'Charlotte Hornets')."""
    tl = (title or "").lower()
    for excl in TITLE_EXCLUSIONS:
        if " " in excl:
            if excl in tl:
                return True
        else:
            if re.search(r"\b" + re.escape(excl) + r"\b", tl):
                return True
    return False


def lot_to_comp(lot, auction, next_id):
    """Map a lots_v2 record + its auction record to a comp row.
    Returns None for unsold/priceless/undated rows. sold_date = the lot's own
    end_timestamp (falls back to the auction's)."""
    if not is_sold(lot):
        return None
    price = lot.get("current_price")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    end_ts = lot.get("end_timestamp") or (auction or {}).get("end_timestamp") or ""
    sold_date = end_ts[:10] if len(end_ts) >= 10 else ""
    if not sold_date:
        return None
    slug = lot.get("meta_slug") or ""
    return {
        "comp_id": "GDV2-%d" % next_id,
        "title": lot.get("title", ""),
        "sold_price": price,
        "sold_date": sold_date,
        "sold_timestamp": end_ts,
        "url": (ITEM_URL_PREFIX + slug) if slug else "",
        "lot_id": lot.get("lot_id", ""),
        "lot_number": lot.get("lot_number", ""),
        "bids": lot.get("number_of_bids", ""),
        "auction_id": lot.get("auction_id") or (auction or {}).get("auction_id", ""),
        "auction_title": (auction or {}).get("title", ""),
        "auction_type": lot.get("auction_type", ""),
        "buyer_premium_pct": lot.get("buyer_premium", ""),
        "primary_image_name": lot.get("primary_image_name", ""),
        "source": "goldin",
        "capture_mode": "auction_scoped_v2",
        "scraped_at": datetime.now().isoformat(),
    }


def merge_auction_lists(payloads):
    """Merge multiple /api/auctions response bodies (site fires several) into
    one auction_id-keyed dict."""
    merged = {}
    for body in payloads or []:
        for a in (body or {}).get("auctions") or []:
            aid = a.get("auction_id")
            if aid:
                merged[aid] = a
    return merged


def extract_lots(payloads, target_auction_id):
    """Pull lot records for one auction out of captured lots_v2 bodies,
    deduped by lot_id. Ignores lots from other auctions (the SPA may fire
    unfiltered queries too)."""
    out = {}
    total_seen = None
    for body in payloads or []:
        sa = (body or {}).get("searchalgolia") or {}
        lots = sa.get("lots") or []
        relevant = [l for l in lots if l.get("auction_id") == target_auction_id]
        if relevant and sa.get("total") is not None:
            total_seen = sa.get("total")
        for l in relevant:
            lid = l.get("lot_id")
            if lid and lid not in out:
                out[lid] = l
    return list(out.values()), total_seen


# ── state / output io ────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(V2_STATE):
        try:
            return json.load(open(V2_STATE))
        except Exception:
            pass
    return {"completed_auctions": {}, "next_comp_id": 1}


def save_state(state):
    tmp = V2_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, V2_STATE)


def load_comps():
    if os.path.exists(V2_FILE):
        try:
            return json.load(open(V2_FILE))
        except Exception:
            pass
    return []


def save_comps(comps):
    tmp = V2_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(comps, f, indent=1)
    os.replace(tmp, V2_FILE)


# ── browser driver ───────────────────────────────────────────────────────────
def run(args):
    from playwright.sync_api import sync_playwright

    state = load_state()
    comps = load_comps()
    known_lot_ids = {c.get("lot_id") for c in comps if c.get("lot_id")}
    next_id = int(state.get("next_comp_id") or 1)

    auction_payloads = []
    lots_payloads = []

    def on_response(resp):
        u = resp.url
        try:
            if "/api/auctions" in u:
                auction_payloads.append(resp.json())
            elif "/api/lots_v2" in u:
                lots_payloads.append(resp.json())
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("response", on_response)

        try:
            # Phase 1: auction calendar
            print("[phase 1] loading auction calendar ...")
            page.goto(BUY_URL, wait_until="domcontentloaded", timeout=60000)
            # NB: must pump the Playwright event loop (wait_for_timeout), NOT
            # time.sleep() — sleeping blocks sync-mode event dispatch and the
            # response handler would never fire.
            deadline = time.time() + 30
            while time.time() < deadline and not auction_payloads:
                page.wait_for_timeout(1000)
            auctions_by_id = merge_auction_lists(auction_payloads)
            print("  auctions on calendar: %d" % len(auctions_by_id))
            if not auctions_by_id:
                print("  FATAL: no /api/auctions payload captured")
                return 1

            if args.auction_id:
                targets = [auctions_by_id[a] for a in [args.auction_id]
                           if a in auctions_by_id]
                if not targets:
                    print("  FATAL: auction id not found on calendar")
                    return 1
            elif getattr(args, "closed_in", None):
                start_iso, _, end_iso = args.closed_in.partition("..")
                targets = pick_auctions_in_window(
                    list(auctions_by_id.values()), start_iso.strip(),
                    end_iso.strip(), state.get("completed_auctions", {}))
            else:
                targets = pick_newest_completed(
                    list(auctions_by_id.values()), args.newest_auctions,
                    state.get("completed_auctions", {}))

            print("  targets (%d):" % len(targets))
            for a in targets:
                print("    %s | ends %s | %s" % (
                    a.get("title", "?"), a.get("end_timestamp", "?"),
                    a.get("auction_id", "?")))
            if args.dry_run:
                print("[dry-run] stopping before lot capture.")
                return 0

            item_type = "" if args.all_item_types else SINGLES_PARAM

            # Phase 2: per-auction sold-lot capture
            for auction in targets:
                aid = auction["auction_id"]
                print("\n[auction] %s (%s)" % (auction.get("title", "?"), aid))
                new_rows = []
                kept = skipped_unsold = skipped_excl = skipped_dupe = 0
                pages_done = 0
                expected_total = None

                for pg in range(1, args.max_pages_per_auction + 1):
                    lots_payloads.clear()
                    url = AUCTION_PAGE_URL.format(
                        item_type=item_type, auction_id=aid, page=pg)
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as e:
                        print("  [page %d] goto error: %s" % (pg, str(e)[:80]))
                        time.sleep(5)
                        continue
                    deadline = time.time() + 25
                    while time.time() < deadline:
                        lots, total = extract_lots(lots_payloads, aid)
                        if lots:
                            break
                        page.wait_for_timeout(1000)  # pumps event loop (see phase-1 note)
                    lots, total = extract_lots(lots_payloads, aid)
                    pages_done = pg
                    if total is not None:
                        expected_total = total
                    if not lots:
                        print("  [page %d] no lots for this auction — stopping" % pg)
                        break

                    page_new = 0
                    for lot in lots:
                        lid = lot.get("lot_id")
                        if not lid or lid in known_lot_ids:
                            skipped_dupe += 1
                            continue
                        known_lot_ids.add(lid)
                        if not is_sold(lot):
                            skipped_unsold += 1
                            continue
                        if is_excluded_title(lot.get("title", "")):
                            skipped_excl += 1
                            continue
                        row = lot_to_comp(lot, auction, next_id)
                        if row is None:
                            skipped_unsold += 1
                            continue
                        next_id += 1
                        new_rows.append(row)
                        kept += 1
                        page_new += 1

                    print("  [page %d] lots=%d new_kept=%d (total_expected=%s)" % (
                        pg, len(lots), page_new, expected_total))

                    # NOTE: do NOT stop on len(known_lot_ids) >= expected_total —
                    # known_lot_ids is the GLOBAL dedup set (all auctions ever), so
                    # once the corpus is large it exceeds any single auction's total
                    # and breaks after page 1. Page-count is the correct terminator.
                    if expected_total is not None:
                        max_pg = math.ceil(expected_total / 240.0)
                        if pg >= max_pg:
                            break
                    page.wait_for_timeout(int(random.uniform(2500, 4000)))

                comps.extend(new_rows)
                save_comps(comps)
                fully_covered = (
                    expected_total is not None
                    and pages_done * 240 >= expected_total)
                if fully_covered and not args.auction_id:
                    state.setdefault("completed_auctions", {})[aid] = {
                        "title": auction.get("title", ""),
                        "end_timestamp": auction.get("end_timestamp", ""),
                        "kept": kept,
                        "done_at": datetime.now().isoformat(),
                    }
                state["next_comp_id"] = next_id
                save_state(state)
                print("  auction done: kept=%d unsold=%d excluded=%d dupes=%d "
                      "pages=%d covered=%s" % (kept, skipped_unsold, skipped_excl,
                                               skipped_dupe, pages_done,
                                               fully_covered))
        finally:
            try:
                browser.close()
            except Exception:
                pass

    print("\n[done] total comps in %s: %d" % (os.path.basename(V2_FILE), len(comps)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--newest-auctions", type=int, default=3,
                    help="capture the N most recently closed auctions not yet done")
    ap.add_argument("--auction-id", help="capture one specific auction id")
    ap.add_argument("--closed-in", metavar="START..END",
                    help="capture ALL completed auctions closing in [START, END), "
                         "e.g. 2025-01-01..2026-01-01 (historical backfill)")
    ap.add_argument("--max-pages-per-auction", type=int, default=20,
                    help="240 lots/page; 20 pages covers auctions up to 4800 lots")
    ap.add_argument("--all-item-types", action="store_true",
                    help="drop the Item_Type=Single Cards filter")
    ap.add_argument("--dry-run", action="store_true",
                    help="list target auctions, capture nothing")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
