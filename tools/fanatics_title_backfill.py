#!/usr/bin/env python3
"""fanatics_title_backfill.py -- pull every Fanatics Collect sale whose title matches a term, across ALL categories.

Why (2026-09-26): the full crawl's CATEGORIES list lacks "Other" and "Wax Non-Sport", where most VeeFriends sales
sit (Fanatics: 341 VeeFriends sales; Neon had ~150). A title shard with no category covers every category.
Reuses the crawler's own normaliser, 1.4M-row dedup index and chunk writer (like fanatics_recent_refresh.py), so
only genuinely new sales land; the sources_supervisor Fanatics->Neon bridge picks the chunks up as usual.
Never flips the crawl's 'completed' flag.

  /usr/bin/python3 tools/fanatics_title_backfill.py --term veefriends [--pages 25] [--dry-run]
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fanatics_full_catalog_scraper_v3 as F  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--term", required=True)
    ap.add_argument("--pages", type=int, default=25, help="20 sales/page; the source caps a query at 50 pages")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    shard = {"sort": "soldDate,desc", "title": a.term}
    total = F.total_from(F.request_json({**shard, "page": 0, "size": 1}))
    print(f"[title-backfill] '{a.term}': {total} sales on Fanatics, walking up to {a.pages} pages", flush=True)
    if total > 20 * min(a.pages, F.SOURCE_PAGE_CAP):
        print(f"  WARNING: {total} > page cap -- older sales need a narrower shard", flush=True)
    if a.dry_run:
        return 0
    conn = F.init_db()
    try:
        state = json.load(open(F.STATE_FILE))
    except Exception:
        state = {"next_chunk": 1}
    completed = state.get("completed")
    st = F.scrape_shard(conn, state, shard, a.sleep, a.pages, 10000)
    state["completed"] = completed
    tmp = str(F.STATE_FILE) + ".tmp"; json.dump(state, open(tmp, "w")); os.replace(tmp, F.STATE_FILE)
    conn.close()
    print(f"[title-backfill done] {st}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
