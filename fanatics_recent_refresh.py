#!/usr/bin/env python3
"""fanatics_recent_refresh.py — capture NEW Fanatics Collect sales, no re-crawl.
=============================================================================
The full catalog crawl (fanatics_full_catalog_scraper_v3) is COMPLETE; re-running
it only re-verifies already-done shards and captures nothing new. This refresh
queries soldDate,desc (newest first) across categories at SHALLOW page depth,
reusing the scraper's OWN dedup index + chunk writer + normalizer — so only
genuinely NEW sales (not in the 1.4M-row sqlite index) land in fresh chunks, which
the existing 24h Neon bridge then picks up. Bounded + idempotent; safe every few h.

Reuses (no edits to the scraper): init_db, base_shards, scrape_shard, CATEGORIES,
AUCTION_TYPES, STATE_FILE, maybe_rotate_chunk (via scrape_shard). scrape_shard
already dedups every row against the shared sqlite index, so re-seeing old sales is
a no-op; --pages caps how deep we walk each shard (soldDate,desc ⇒ newest on page 0).

Usage:
  python3 fanatics_recent_refresh.py --dry-run
  python3 fanatics_recent_refresh.py --pages 5
  python3 fanatics_recent_refresh.py --categories Basketball Pokémon --pages 3
"""
import argparse
import json
import os
import sys

import fanatics_full_catalog_scraper_v3 as F


def load_state():
    try:
        return json.load(open(F.STATE_FILE))
    except Exception:
        return {"next_chunk": 1}


def save_state(state):
    tmp = str(F.STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, F.STATE_FILE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=5,
                    help="pages/shard walked newest-first (20 sales/page)")
    ap.add_argument("--categories", nargs="*", default=None,
                    help="subset (default: all F.CATEGORIES)")
    ap.add_argument("--auction-types", nargs="*", default=None)
    ap.add_argument("--rows-per-chunk", type=int, default=10000)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--title-pages", type=int, default=3, help="pages walked per extra title shard")
    a = ap.parse_args()

    # Categories the crawl never listed (2026-09-27): Fanatics also files sales under "Other" and "Wax*" (80,722 sales).
    try:
        extra_cats = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fanatics_extra_categories.json")))
    except Exception:
        extra_cats = []
    cats = a.categories or (F.CATEGORIES + [c for c in extra_cats if c not in F.CATEGORIES])
    ats = a.auction_types or F.AUCTION_TYPES
    shards = F.base_shards(cats, ats, "soldDate,desc")
    # Extra TITLE shards (2026-09-26): products whose sales sit in categories the crawl never lists ("Other",
    # "Wax Non-Sport") -- e.g. VeeFriends: Fanatics had 341 sales, we held 159. A title shard with no category
    # covers every category. Terms live in fanatics_extra_title_shards.json (["veefriends"]).
    titles_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fanatics_extra_title_shards.json")
    try:
        extra_titles = json.load(open(titles_file))
    except Exception:
        extra_titles = []
    title_shards = [{"sort": "soldDate,desc", "title": t} for t in extra_titles]
    # PRICE shards (2026-09-29): newest sales in each price band across ALL categories, 50 rows/page. The API returns at
    # most 999 rows per query (probed 09-29), so $1K+ is split into bands, each read 999 deep every refresh (each band's
    # newest 999 reached back 4-8 days on 09-29). A Premier auction closes thousands of lots at once; the per-category
    # 5-page window missed the $8.04M Flagg, $2.88M Harper and $2.34M Knueppel debut patches.
    # fanatics_extra_price_shards.json: a floor (newest sales >= it) or a [lo, hi] band (hi null = no ceiling).
    try:
        price_floors = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fanatics_extra_price_shards.json")))
    except Exception:
        price_floors = []
    price_shards = []
    for f in price_floors:
        lo, hi = f if isinstance(f, list) else (f, None)
        price_shards.append({"sort": "soldDate,desc", "priceMin": int(lo), **({"priceMax": int(hi)} if hi is not None else {})})
    print("[refresh] %d shards (%d cats x %d types), pages/shard=%d; +%d title shards %s at %d pages" % (
        len(shards), len(cats), len(ats), a.pages, len(title_shards), extra_titles, a.title_pages), flush=True)

    if a.dry_run:
        for s in title_shards:
            print("  would refresh title shard:", s)
        for s in shards[:6]:
            print("  would refresh:", s)
        if len(shards) > 6:
            print("  ... (%d shards total)" % len(shards))
        return 0

    conn = F.init_db()
    state = load_state()
    completed_flag = state.get("completed")  # refresh must never flip 'completed'
    tot = {"written": 0, "dupe": 0, "seen": 0, "rejected": 0}
    for shard in price_shards + title_shards + shards:
        try:
            pages = a.title_pages if shard in title_shards else a.pages
            if shard in price_shards:
                size_was, F.PAGE_SIZE, pages = F.PAGE_SIZE, 50, F.SOURCE_PAGE_CAP
                try:
                    st = F.scrape_shard(conn, state, shard, a.sleep, pages, a.rows_per_chunk)
                finally:
                    F.PAGE_SIZE = size_was
            else:
                st = F.scrape_shard(conn, state, shard, a.sleep, pages, a.rows_per_chunk)
        except Exception as exc:
            print("  [shard err] %s: %s" % (shard, str(exc)[:110]), flush=True)
            continue
        for k in tot:
            tot[k] += st.get(k, 0)
        if st.get("written"):
            print("  +%d new  %s" % (st["written"], shard), flush=True)
        state["completed"] = completed_flag
        save_state(state)
    conn.close()
    print("[refresh done] written=%d dupe=%d seen=%d rejected=%d across %d shards" % (
        tot["written"], tot["dupe"], tot["seen"], tot["rejected"], len(shards)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
