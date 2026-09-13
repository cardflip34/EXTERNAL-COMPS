#!/usr/bin/env python3
"""Incremental image sync — keeps the comp-image archive tracking the LIVE scrubs (runs in every
6-hourly canonical_refresh cycle, after ingest/classify).

For each image-bearing source it diffs canonical against the download ledger and fetches only what is
genuinely new or previously-retryable:
  settled  = ledger status in (ok, skip, dead, small)      -> never touched again
  retryable= err / neterr / blocked                        -> re-attempted next cycle
While the ONE-TIME bulk backfill (`comp_image_backfill.py --all`) is still running, sources it owns
(ebay, fanatics, scp_catalog) are exported-only here (no competing downloader on the same hosts);
tcgplayer_catalog — a different CDN the bulk run never touches — downloads immediately.
Single-instance via pid lockfile. Deltas + logs live in comp_images/_backfill/.
Usage: .venv_extcomps/bin/python external_engine/image_sync_incremental.py [--store DIR] [--rate 4]
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time

import duckdb

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
BASE = "/Volumes/MAZI_EVIDENCE_6TB/comp_images"; W = os.path.join(BASE, "_backfill")
PY_SYS = "/usr/bin/python3"
HERE = os.path.dirname(os.path.abspath(__file__))
SETTLED = ("ok", "skip", "dead", "small")

# key/url expressions per source, matching the bulk exporter exactly
def source_queries(rel: str) -> dict:
    return {
        "ebay": f"SELECT source_item_id AS key, max(image_url) AS url FROM {rel} WHERE source='ebay' AND image_url LIKE 'http%' GROUP BY 1",
        "fanatics": f"SELECT replace(source_item_id, ':', '_') AS key, max(image_url) AS url FROM {rel} WHERE source='fanatics' AND image_url LIKE 'http%' GROUP BY 1",
        "scp_catalog": f"SELECT substr(sha1(image_url),1,20) AS key, image_url AS url FROM (SELECT DISTINCT image_url FROM {rel} WHERE source='sportscardspro' AND image_url LIKE 'http%')",
        "tcgplayer_catalog": f"SELECT regexp_extract(image_url,'/([0-9]+)\\.jpg',1) AS key, max(image_url) AS url FROM {rel} WHERE source='tcgplayer' AND image_url LIKE 'http%' GROUP BY 1",
    }

def bulk_backfill_running() -> bool:
    r = subprocess.run(["/usr/bin/pgrep", "-f", "comp_image_backfill.py --all"], capture_output=True, text=True)
    return bool(r.stdout.strip())

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--store", default=STORE); ap.add_argument("--rate", type=float, default=4.0)
    a = ap.parse_args()
    os.makedirs(W, exist_ok=True)
    lock = os.path.join(W, ".sync.lock")
    if os.path.exists(lock):
        pid = open(lock).read().strip()
        if pid.isdigit() and subprocess.run(["/bin/kill", "-0", pid], capture_output=True).returncode == 0:
            print(f"[image-sync] already running pid={pid}; skipping"); return
    open(lock, "w").write(str(os.getpid()))
    try:
        P = os.path.join(a.store, "external_store", "parquet")
        rel = f"read_parquet('{P}/**/*.parquet', hive_partitioning=true, union_by_name=true)"
        con = duckdb.connect(); con.execute("SET memory_limit='800MB'; SET threads=1")
        bulk = bulk_backfill_running()
        summary = {}
        for src, q in source_queries(rel).items():
            led = os.path.join(W, f"ledger_{src}.jsonl")
            settled = "', '".join(SETTLED)
            if os.path.exists(led):
                led_rel = (f"(SELECT k FROM (SELECT k, s, row_number() OVER (PARTITION BY k ORDER BY rowid DESC) rn "
                           f"FROM (SELECT row_number() OVER () rowid, * FROM read_json('{led}', format='newline_delimited', "
                           f"columns={{'k':'VARCHAR','s':'VARCHAR','c':'INT','b':'BIGINT'}}))) WHERE rn=1 AND s IN ('{settled}'))")
            else:
                led_rel = "(SELECT NULL AS k WHERE false)"
            delta_path = os.path.join(W, f"candidates_{src}_delta.csv")
            con.execute(f"""COPY (SELECT c.key, c.url FROM ({q}) c
                            WHERE c.key IS NOT NULL AND c.key NOT IN (SELECT k FROM {led_rel} WHERE k IS NOT NULL))
                            TO '{delta_path}' (HEADER false)""")
            n = sum(1 for _ in open(delta_path))
            owned_by_bulk = bulk and src in ("ebay", "fanatics", "scp_catalog")
            summary[src] = {"delta": n, "action": "export-only (bulk backfill owns this host)" if owned_by_bulk else ("download" if n else "none")}
            print(f"[image-sync] {src}: delta={n:,} -> {summary[src]['action']}", flush=True)
            if n and not owned_by_bulk:
                rc = subprocess.run([PY_SYS, "-u", os.path.join(HERE, "comp_image_backfill.py"),
                                     "--source", src, "--rate", str(a.rate), "--candidates", delta_path, "--no-yield"]).returncode
                summary[src]["rc"] = rc
        json.dump({"at": time.strftime("%FT%TZ", time.gmtime()), "bulk_running": bulk, "sources": summary},
                  open(os.path.join(W, "image_sync_last.json"), "w"), indent=1)
    finally:
        os.remove(lock)

if __name__ == "__main__":
    main()
