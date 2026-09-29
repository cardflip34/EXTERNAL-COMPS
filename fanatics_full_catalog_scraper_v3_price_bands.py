#!/usr/bin/env python3
"""fanatics_full_catalog_scraper_v3_price_bands.py -- fill what the v3 Fanatics crawl could not reach (2026-09-27).

Measured 2026-09-27: Fanatics serves 5,195,626 sales; ~1.46M are in Neon.
  * The public sales-history API serves at most 999 rows per query, whatever the page size (re-measured 2026-09-29; the
    09-27 note here said 50 pages x 50 = 2,500, which was wrong). v3 asked 20 rows/page and split big shards by title
    words; 1,848 shards stayed "over_cap_after_all_splits". Categories "Other" and "Wax*" were never listed at all.
  * This tool asks 50 rows/page (fewer requests) and splits any query by WHOLE-DOLLAR PRICE BANDS (priceMin inclusive,
    priceMax exclusive -- verified to partition exactly; decimal bounds return 0) until each holds <= 999 sales.
  * Rows go through the crawler's own normaliser, 1.4M-key dedup index and chunk writer; the sources_supervisor
    Fanatics->Neon bridge lands them as usual.
  * The crawler's maybe_rotate_chunk re-reads the whole current chunk for EVERY row (quadratic on the busy 6TB disk);
    here a running per-chunk counter replaces that read, same rotation rule. The v3 module itself is not modified.
  * The file name contains 'fanatics_full_catalog_scraper_v3' ON PURPOSE: tools/sources_supervisor.py treats any such
    process as the Fanatics writer, so the 6-hourly refresh (same chunk state + index) pauses while this runs.

  python3 fanatics_full_catalog_scraper_v3_price_bands.py --categories Other --dry-run
  python3 fanatics_full_catalog_scraper_v3_price_bands.py --categories Other "Wax Non-Sport"
  python3 fanatics_full_catalog_scraper_v3_price_bands.py --from-unresolved --only-category Pokémon --dry-run
"""
import argparse, json, os, random, sys, time
from pathlib import Path
import fanatics_full_catalog_scraper_v3 as F

F.PAGE_SIZE = 50                                   # the source's maximum page size
# Rows reachable per query. The API stops at 999 rows whatever the page size (probed 2026-09-29: size 50 page 19 = 49
# rows, page 20 = 0; size 20 page 49 = 19, page 50 = 0). This was SOURCE_PAGE_CAP * PAGE_SIZE = 2,500, so bands of
# 1,000-2,500 sales were never split: 151 of the 250 bands in the 09-29 $1K+ sweep stopped at 999 (~170K sales unread).
PER_QUERY = 999
PROGRESS = F.OUT_DIR / "price_band_progress.jsonl"

_counts = {}
_orig_append = F.append_jsonl
def _rotate(state, rows_per_chunk):
    path = F.chunk_path(state)
    if path not in _counts:
        _counts[path] = sum(1 for _ in path.open()) if path.exists() else 0
    if _counts[path] >= rows_per_chunk:
        state["next_chunk"] = int(state.get("next_chunk") or 1) + 1
        F.save_json_atomic(F.STATE_FILE, state)
        path = F.chunk_path(state)
        _counts.setdefault(path, sum(1 for _ in path.open()) if path.exists() else 0)
    return path
def _append(path, payload):
    _orig_append(path, payload)
    _counts[path] = _counts.get(path, 0) + 1
F.maybe_rotate_chunk = _rotate
F.append_jsonl = _append


def count(base, lo, hi):
    q = {**base, "priceMin": lo, "page": 0, "size": 1}
    if hi is not None:
        q["priceMax"] = hi
    time.sleep(0.5)
    return F.total_from(F.request_json(q))


def plan(base, log, floor=0):
    """Yield (lo, hi, n) price bands of `base` from `floor` up, each <= PER_QUERY rows where possible."""
    total = count(base, floor, None)
    if total <= PER_QUERY:
        yield (floor, None, total); return
    hi = max(64, floor * 2)                                        # find a finite ceiling that leaves the open tail small
    while count(base, hi, None) > PER_QUERY and hi < 10_000_000:
        hi *= 4
    tail = count(base, hi, None)
    if tail:
        yield (hi, None, tail)
    stack = [(floor, hi)]
    while stack:
        lo, h = stack.pop()
        n = count(base, lo, h)
        if n == 0:
            continue
        if n <= PER_QUERY or h - lo <= 1:
            if n > PER_QUERY:
                log(f"  [dense] {base} ${lo}-{h}: {n:,} > {PER_QUERY} -- newest {PER_QUERY} only")
            yield (lo, h, n)
        else:
            mid = (lo + h) // 2
            stack += [(mid, h), (lo, mid)]


def bases_from_unresolved(only_category):
    seen, out = set(), []
    for l in open(F.UNRESOLVED_FILE):
        r = json.loads(l)
        if only_category and r.get("category") != only_category:
            continue
        b = {k: r[k] for k in ("category", "auctionTypes", "gradingService", "yearMin", "yearMax") if r.get(k) not in (None, "")}
        key = json.dumps(b, sort_keys=True)
        if key not in seen:
            seen.add(key); out.append(b)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", nargs="*", default=[])
    ap.add_argument("--from-unresolved", action="store_true")
    ap.add_argument("--only-category")
    ap.add_argument("--max-bases", type=int)
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    # 2026-09-29 HIGH-VALUE sweep: no category (all of Fanatics) from --min-price up. The 6-hourly refresh reads only the
    # newest ~250 sales per category, so a Premier auction closing thousands of lots at once outran it (Flagg $8.04M missed).
    ap.add_argument("--no-category", action="store_true", help="one base covering every category")
    ap.add_argument("--min-price", type=int, default=0)
    a = ap.parse_args()
    log = lambda m: print(m, flush=True)
    bases = [{"category": c} for c in a.categories] + ([{}] if a.no_category else [])
    if a.from_unresolved:
        bases += bases_from_unresolved(a.only_category)
    bases = bases[:a.max_bases] if a.max_bases else bases
    done = {l.strip() for l in open(PROGRESS)} if PROGRESS.exists() else set()
    conn = None if a.dry_run else F.init_db()
    try:
        state = json.load(open(F.STATE_FILE))
    except Exception:
        state = {"next_chunk": 1}
    completed = state.get("completed")
    grand = {"bands": 0, "rows_listed": 0, "written": 0, "dupe": 0, "pages": 0}
    for base in bases:
        bands = list(plan(base, log, a.min_price))
        listed = sum(n for *_, n in bands)
        log(f"[base] {base}: {len(bands)} bands, {listed:,} sales, ~{sum(-(-min(n, PER_QUERY) // F.PAGE_SIZE) for *_, n in bands):,} pages")
        grand["bands"] += len(bands); grand["rows_listed"] += listed
        if a.dry_run:
            continue
        for lo, hi, n in bands:
            key = json.dumps({**base, "lo": lo, "hi": hi}, sort_keys=True)
            if key in done:
                continue
            shard = {**base, "sort": "soldDate,desc", "priceMin": lo, **({"priceMax": hi} if hi is not None else {})}
            try:
                st = F.scrape_shard(conn, state, shard, a.sleep, None, 10000)
            except Exception as e:
                log(f"  [band err] {key}: {str(e)[:160]}"); continue
            for k in ("written", "dupe", "pages"):
                grand[k] += st.get(k, 0)
            state["completed"] = completed
            F.save_json_atomic(F.STATE_FILE, state)
            with open(PROGRESS, "a") as f:
                f.write(key + "\n")
            log(f"  band ${lo}-{hi if hi is not None else 'inf'}: listed {n:,} written {st.get('written', 0):,} dupe {st.get('dupe', 0):,}")
    if conn:
        conn.close()
    log(f"[done] {grand}")


if __name__ == "__main__":
    sys.exit(main())
