#!/usr/bin/env python3
"""Lane runners (PRD V2 §10/§13/§14) — freshness + backfill, polite eBay adapter → staging → canonical writer → ledger.

Both refuse to start unless source_health allows eBay contact (BLOCKED → exit 3). Both are canary-first:
    --limit N  bounds the number of subjects/players this invocation touches.

FRESHNESS (one-day window, completed-ranks-first subjects):
  .venv_extcomps/bin/python external_engine/lanes.py freshness --date 2026-08-18 --limit 5 [--max-pages 2]
      subjects default = ledger roster completed players (rank order) → "<name> card" searches for that day.
BACKFILL (pending top-1000 players via the query matrix, polite):
  .venv_extcomps/bin/python external_engine/lanes.py backfill --limit 3 [--max-queries 20] [--max-pages 2]
      players = ledger entries with status pending|failed_retry|running (rank order). Ledger entries are NOT
      mutated by this runner (status stays the legacy phase ledger's business) — the V2 run ledger records coverage.
Every subject/player = one ledger run with checkpoints; rows staged per query; ingested by the canonical writer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)

import source_health as sh  # noqa: E402
from ebay_polite_adapter import EbayPoliteAdapter, SourceBlocked, SourceUnavailable  # noqa: E402

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
LEDGER_FILE = os.path.join(STORE, "player_scrub_phase_ledger.json")
CODE_VERSION = "lanes-0.1.0"


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def roster(status_filter: set[str] | None) -> list[dict]:
    with open(LEDGER_FILE) as f:
        entries = json.load(f).get("entries", [])
    if status_filter:
        entries = [e for e in entries if e.get("status") in status_filter]
    entries = [e for e in entries if e.get("player_name")]
    return sorted(entries, key=lambda e: int(e.get("rank") or 10**6))


def _writer():
    from canonical_writer import CanonicalWriter
    return CanonicalWriter(store=STORE, memory_limit="600MB")


def freshness(date: str, limit: int, max_pages: int, subjects: list[str] | None, dry_run: bool) -> int:
    ok, why = sh.contact_allowed("ebay", STORE)
    if not ok:
        _log(f"REFUSED: {why}"); return 3
    if subjects:
        subs = subjects
    else:
        # Standing non-player targets first: they are few, and they are the ones nothing else covers.
        # The roster is sports players only, so before this a category like VeeFriends was scrubbed
        # only by accident — every VeeFriends row in canonical arrived under some other query.
        import scrub_plan
        subs = scrub_plan.subjects() + [e["player_name"] for e in
                                        roster({"completed_existing", "completed_phase_scrub"})]
    subs = subs[:limit]
    w = _writer(); L = w.ledger
    done = 0; staged_rows = 0; ingested = 0
    try:
        with EbayPoliteAdapter(store=STORE, max_pages=max_pages) as ad:
            for s in subs:
                qid = f"freshness:subject:{s}"
                if L.window_done("ebay", qid, date, date):
                    _log(f"skip {s}: window {date} already completed"); continue
                rid = L.start_run("ebay", "freshness", query_id=qid, subject=s, window_start=date, window_end=date, code_version=CODE_VERSION)
                try:
                    res = ad.search(f"{s} card", window_day=date, max_pages=max_pages, query_label="freshness:day")
                    L.checkpoint(rid, {"pages": res["pages"]}, pages=res["pages"], rows_seen=res["seen"])
                    path = ad.stage(res["rows"], "freshness", qid, date)
                    staged_rows += len(res["rows"])
                    if res["rows"] and not dry_run:
                        rep = w.ingest(path, source_label="ebay_polite_v2", lane="freshness", query_id=qid, subject=s,
                                       window_start=date, window_end=date, code_version=CODE_VERSION)
                        ingested += rep["rows_new"]
                        L.complete(rid, pages=res["pages"], rows_seen=res["seen"], rows_new=rep["rows_new"], rows_duplicate=rep["rows_duplicate"])
                    else:
                        L.complete(rid, pages=res["pages"], rows_seen=res["seen"], rows_new=0)
                    done += 1
                    _log(f"{s}: pages={res['pages']} seen={res['seen']} kept={len(res['rows'])} early_stop={res['early_stop']}")
                    ad.backoff()
                except SourceBlocked as e:
                    L.fail(rid, str(e), retry=True, blocked=True); _log(f"BLOCKED on {s}: {e} — lane stops"); return 4
                except SourceUnavailable as e:
                    L.fail(rid, str(e), retry=True); _log(f"unavailable on {s}: {e} — lane stops"); return 5
                except Exception as e:
                    L.fail(rid, f"{type(e).__name__}: {e}", retry=True); _log(f"error on {s}: {e}")
    finally:
        w.close()
    _log(f"freshness done: subjects={done} staged_rows={staged_rows} ingested_new={ingested}")
    return 0


def backfill(limit: int, max_queries: int, max_pages: int, dry_run: bool) -> int:
    ok, why = sh.contact_allowed("ebay", STORE)
    if not ok:
        _log(f"REFUSED: {why}"); return 3
    from ebay_player_scraper import build_query_matrix
    players = roster({"pending", "failed_retry", "running"})[:limit]
    w = _writer(); L = w.ledger
    try:
        with EbayPoliteAdapter(store=STORE, max_pages=max_pages) as ad:
            for e in players:
                name = e["player_name"]; qid = f"backfill:player:{name}"
                rid = L.start_run("ebay", "backfill", query_id=qid, subject=name, code_version=CODE_VERSION,
                                  extra={"rank": e.get("rank"), "phase": e.get("phase")})
                queries = build_query_matrix(name)[:max_queries]
                seen = new = dup = 0; pages = 0
                try:
                    for qi, (q, label) in enumerate(queries, 1):
                        res = ad.search(q, window_day=None, max_pages=max_pages, query_label=label)
                        seen += res["seen"]; pages += res["pages"]
                        if res["rows"]:
                            path = ad.stage(res["rows"], "backfill", f"{qid}:{label}")
                            if not dry_run:
                                rep = w.ingest(path, source_label="ebay_polite_v2", lane="backfill", query_id=f"{qid}:{label}", subject=name, code_version=CODE_VERSION)
                                new += rep["rows_new"]; dup += rep["rows_duplicate"]
                        L.checkpoint(rid, {"query_index": qi, "last_query": q}, pages=pages, rows_seen=seen, rows_new=new, rows_duplicate=dup)
                        ad.backoff()
                    L.complete(rid, pages=pages, rows_seen=seen, rows_new=new, rows_duplicate=dup)
                    _log(f"{name}: queries={len(queries)} pages={pages} seen={seen} new={new} dup={dup}")
                except SourceBlocked as ex:
                    L.fail(rid, str(ex), retry=True, blocked=True); _log(f"BLOCKED on {name}: {ex} — lane stops"); return 4
                except SourceUnavailable as ex:
                    L.fail(rid, str(ex), retry=True); _log(f"unavailable on {name}: {ex} — lane stops"); return 5
    finally:
        w.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freshness"); f.add_argument("--date", required=True); f.add_argument("--limit", type=int, default=5)
    f.add_argument("--max-pages", type=int, default=2); f.add_argument("--subjects", default=None, help="comma list (default: completed roster)")
    f.add_argument("--dry-run", action="store_true")
    b = sub.add_parser("backfill"); b.add_argument("--limit", type=int, default=3); b.add_argument("--max-queries", type=int, default=20)
    b.add_argument("--max-pages", type=int, default=2); b.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "freshness":
        sys.exit(freshness(a.date, a.limit, a.max_pages, a.subjects.split(",") if a.subjects else None, a.dry_run))
    sys.exit(backfill(a.limit, a.max_queries, a.max_pages, a.dry_run))


if __name__ == "__main__":
    main()
