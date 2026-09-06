#!/usr/bin/env python3
"""Neon → staging export for sources whose live pipeline writes Neon-only (PRD V2: canonical must track live).

Built 2026-09-06 for Fanatics: its v3 pipeline lands rows in Neon (1.42M, current) while canonical was reading
a legacy JSON snapshot frozen Jun 22 (44K) — a 76-day lag the freshness report caught on its first run.

Incremental, SCP-style: appends canonical-friendly JSONL to <store>/external_store/staging/<source>_neon.jsonl
(on the 6TB) using a scraped_at cursor; first run exports everything. Boundary rows re-export (cursor is >=)
— downstream ingest dedups by (observation_id, sold_date), so duplicates in the file are harmless.
Runs under /usr/bin/python3 (psycopg); needs MAZI_DB_URL in the environment.

Usage:  python3 external_engine/neon_source_export.py --source fanatics [--store DIR] [--batch 20000]
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone
import psycopg

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="fanatics")
    ap.add_argument("--store", default=STORE)
    ap.add_argument("--batch", type=int, default=20000)
    a = ap.parse_args()
    url = os.environ.get("MAZI_DB_URL")
    if not url:
        print("[export] MAZI_DB_URL not set — skipping (no failure: ingest just reuses the existing staging file)")
        return 0
    stage_dir = os.path.join(a.store, "external_store", "staging")
    os.makedirs(stage_dir, exist_ok=True)
    out_path = os.path.join(stage_dir, f"{a.source}_neon.jsonl")
    cur_path = os.path.join(stage_dir, f"{a.source}_neon.cursor.json")
    cursor = None
    try:
        cursor = json.load(open(cur_path)).get("max_scraped_at")
    except (OSError, ValueError):
        pass
    n = 0
    max_seen = cursor
    with psycopg.connect(url, connect_timeout=25) as con:
        with con.cursor(name="neon_export") as cur:  # server-side cursor: streams, bounded memory
            cur.itersize = a.batch
            q = """SELECT source_item_id, title, sold_price, sold_date::date, source_url, best_offer,
                          grade, scraped_at, raw
                   FROM external_transactions WHERE source_code = %s"""
            params = [a.source]
            if cursor:
                q += " AND scraped_at >= %s"
                params.append(cursor)
            q += " ORDER BY scraped_at NULLS FIRST"
            cur.execute(q, params)
            with open(out_path, "a") as f:
                for sid, title, price, sdate, surl, bo, grade, scraped, raw in cur:
                    if isinstance(raw, str):
                        try: raw = json.loads(raw)
                        except ValueError: raw = {}
                    raw = raw or {}
                    row = {"comp_id": sid, "title": title, "sold_price": str(price) if price is not None else None,
                           "sold_date": str(sdate) if sdate else None, "url": surl,
                           "image_url": raw.get("image_url"), "grade": grade, "grader": raw.get("grader"),
                           "best_offer": str(bo) if bo is not None else None,
                           "buyers_premium": str(raw.get("buyers_premium")) if raw.get("buyers_premium") is not None else None,
                           "scraped_at": scraped.isoformat() if scraped else None}
                    f.write(json.dumps(row, default=str) + "\n")
                    n += 1
                    if scraped and (max_seen is None or str(scraped) > str(max_seen)):
                        max_seen = scraped.isoformat() if hasattr(scraped, "isoformat") else str(scraped)
    if max_seen and max_seen != cursor:
        tmp = cur_path + ".tmp"
        json.dump({"max_scraped_at": str(max_seen), "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, open(tmp, "w"))
        os.replace(tmp, cur_path)
    print(f"[export] {a.source}: +{n:,} rows appended (cursor {cursor} -> {max_seen}) -> {out_path}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
