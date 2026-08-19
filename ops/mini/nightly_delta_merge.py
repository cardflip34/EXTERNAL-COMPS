#!/usr/bin/env python3
"""
nightly_delta_merge.py — window-filter + provenance-stamp + dedup-merge eBay
nightly daily-delta comp rows into the existing external-comps store.

DRY-RUN BY DEFAULT: prints what it would do and writes nothing. Pass --apply to
write. The apply is CLOBBER-SAFE: it re-checks the store's (mtime, size) and
retries if a concurrent writer (e.g. the phase backfill rewriting the same
player_comps.json) touched the file while we worked, so it never overwrites
another process's rows. The run file is preserved, so a failed apply loses
nothing.

Pipeline:
  1. load freshly captured nightly run rows           (--in)
  2. keep only rows whose sold_date is the window day  (--capture-date)
  3. stamp additive provenance: source, capture_date, captured_at, and a
     deterministic comp_id = PLN-<itemid> (each only if absent)
  4. dedup by eBay item ID (/itm/<id> in url) against the existing --store
  5. clobber-safe append: write a temp copy, re-check the store didn't change,
     then atomic os.replace; reload + retry on concurrent change

Same store/schema as the phase scrubs (generate_feed.load_player_comp_index):
run it once for the graded store (player_comps.json) and once for the raw store
(raw_player_comps.json).
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, date, timedelta

ITM_RE = re.compile(r"/itm/(\d+)")

# Tolerant eBay sold-date parsing -> datetime.date or None.
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d %Y")
_DATE_PREFIXES = ("Sold  ", "Sold ", "sold ", "Ended  ", "Ended ", "ended ")


def parse_sold_date(value):
    if not value:
        return None
    text = str(value).strip()
    for pre in _DATE_PREFIXES:
        if text.startswith(pre):
            text = text[len(pre):].strip()
            break
    try:  # ISO timestamp / date
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def item_id(row):
    m = ITM_RE.search(row.get("url", "") or "")
    return m.group(1) if m else None


def load_json_list(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            sys.exit(f"ERROR: {path} is not valid JSON ({exc}); refusing to merge.")
    if not isinstance(data, list):
        sys.exit(f"ERROR: {path} is not a JSON list.")
    return data


def store_sig(path):
    """(mtime_ns, size) of the store, or None if absent — used to detect a
    concurrent writer (e.g. the phase backfill) modifying the same file."""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def dump_temp(target_path, data):
    """Serialize data to a temp file in the target's directory; return its path."""
    directory = os.path.dirname(os.path.abspath(target_path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".nightly_merge.", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    return tmp


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="infile", required=True,
                    help="freshly captured nightly run rows (JSON list)")
    ap.add_argument("--store", required=True,
                    help="canonical store to merge into (player_comps.json or raw_player_comps.json)")
    ap.add_argument("--capture-date",
                    default=(date.today() - timedelta(days=1)).isoformat(),
                    help="window day YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--source", default="nightly_cron",
                    help="provenance tag stamped on each row (default: nightly_cron)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write the store (default: dry-run, writes nothing)")
    args = ap.parse_args()

    try:
        window = datetime.strptime(args.capture_date, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"ERROR: --capture-date must be YYYY-MM-DD, got {args.capture_date!r}")

    run_rows = load_json_list(args.infile)

    # Build candidates once: window-valid, provenance-stamped, deduped within the
    # run file. Dedup against the *store* is recomputed per attempt below, since a
    # concurrent writer may add matching item IDs while we work.
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    candidates = []
    seen_new = set()
    skip_window = skip_nodate = 0
    for row in run_rows:
        sold = parse_sold_date(row.get("sold_date"))
        if sold is None:
            skip_nodate += 1
            continue
        if sold != window:
            skip_window += 1
            continue
        iid = item_id(row)
        if iid and iid in seen_new:
            continue  # duplicate within this run file
        if iid:
            seen_new.add(iid)
        # additive provenance — never overwrite an existing value
        row.setdefault("source", args.source)
        row.setdefault("capture_date", args.capture_date)
        row.setdefault("captured_at", now_iso)
        if iid:
            row.setdefault("comp_id", f"PLN-{iid}")  # idempotent; PLN- = nightly provenance
        candidates.append(row)

    def dedup_against(rows):
        existing = {iid for iid in (item_id(r) for r in rows) if iid}
        return [c for c in candidates if not (item_id(c) and item_id(c) in existing)]

    store_rows = load_json_list(args.store)
    kept = dedup_against(store_rows)
    print(f"window day        : {args.capture_date}")
    print(f"store             : {args.store}")
    print(f"run rows in       : {len(run_rows):,}")
    print(f"  in window       : {len(candidates):,}")
    print(f"  kept (new)      : {len(kept):,}")
    print(f"  skip out-of-win : {skip_window:,}")
    print(f"  skip dup item   : {len(candidates) - len(kept):,}")
    print(f"  skip no sold_dt : {skip_nodate:,}")
    print(f"store before      : {len(store_rows):,}  ->  after: {len(store_rows) + len(kept):,}")
    print(f"source stamp      : {args.source}")

    if not args.apply:
        print("\nDRY-RUN: nothing written. Re-run with --apply to merge.")
        return

    # Clobber-safe apply: the phase backfill may be rewriting this same store file
    # concurrently. Re-load + re-dedup, write a temp, and only os.replace if the
    # store's (mtime, size) did not change while we worked; otherwise reload and
    # retry. Guarantees we never overwrite a concurrent writer's rows.
    for attempt in range(1, 6):
        sig0 = store_sig(args.store)
        store_rows = load_json_list(args.store)
        kept = dedup_against(store_rows)
        tmp = dump_temp(args.store, store_rows + kept)
        if store_sig(args.store) != sig0:
            os.remove(tmp)
            print(f"store changed under us (attempt {attempt}/5); reloading to avoid clobber")
            time.sleep(1)
            continue
        os.replace(tmp, args.store)
        print(f"\nAPPLIED: +{len(kept):,} new rows -> {len(store_rows) + len(kept):,} total in {args.store}")
        return
    sys.exit("ERROR: store kept changing; aborted WITHOUT writing (run file preserved). Retry when the store is idle.")


if __name__ == "__main__":
    main()
