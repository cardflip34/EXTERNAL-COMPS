#!/usr/bin/env python3
"""Freshness-lag report (PRD V2 §11) — per-source and per-category lag, written as a SMALL artifact.

Runs inside the 6-hourly canonical refresh (lane venv, DuckDB over canonical parquet). The 20-min heartbeat
only READS the JSON this writes — heavy compute stays out of the monitoring path. Lag = days between today
and the newest sold_date we hold. Expected lags differ by source (TCGplayer same-day; SCP posts sales ~3-7
days late; auction houses weekly), so per-source thresholds live here, not one global number.

Writes: <store>/external_store/freshness/freshness_latest.json
Usage:  .venv_extcomps/bin/python external_engine/freshness_report.py [--store DIR]
"""
from __future__ import annotations
import argparse, json, os
from datetime import date, datetime, timezone
import duckdb

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
# expected_lag_days: alert only when lag exceeds this (source-appropriate, not one-size-fits-all)
EXPECTED = {"tcgplayer": 2, "myslabs": 3, "sportscardspro": 10, "auctionreport": 7,
            "goldin": 14, "fanatics": 21, "rea": 90, "ebay": 99999}  # ebay: blocked, excluded from alerting
CATEGORY_OF = {"tcgplayer": "pokemon_tcg", "sportscardspro": "sports", "myslabs": "sports_graded",
               "goldin": "auction_high_end", "fanatics": "auction_high_end", "rea": "auction_vintage",
               "auctionreport": "auction_top_lots", "ebay": "blocked_frozen"}

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--store", default=STORE); a = ap.parse_args()
    P = os.path.join(a.store, "external_store", "parquet")
    out_dir = os.path.join(a.store, "external_store", "freshness"); os.makedirs(out_dir, exist_ok=True)
    con = duckdb.connect(); con.execute("SET memory_limit='600MB'; SET threads=1")
    rel = f"read_parquet('{P}/**/*.parquet', hive_partitioning=true, union_by_name=true)"
    rows = con.execute(f"SELECT source, max(sold_date), count(*) FROM {rel} GROUP BY 1").fetchall()
    today = date.today()
    sources, alerts = {}, []
    for s, mx, n in rows:
        lag = (today - mx).days if mx else None
        exp = EXPECTED.get(s, 14)
        status = "frozen" if s == "ebay" else ("ok" if lag is not None and lag <= exp else "LAGGING")
        sources[s] = {"rows": n, "latest_sale": str(mx), "lag_days": lag,
                      "expected_max_days": exp, "category": CATEGORY_OF.get(s, "other"), "status": status}
        if status == "LAGGING":
            alerts.append(f"{s} lag {lag}d > expected {exp}d")
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "as_of_date": str(today), "sources": sources, "alerts": alerts,
           "note": "lag = today - newest sold_date in canonical; expected lags are source-native cadences"}
    p = os.path.join(out_dir, "freshness_latest.json")
    with open(p + ".tmp", "w") as f: json.dump(doc, f, indent=1)
    os.replace(p + ".tmp", p)
    print(json.dumps({k: f"{v['lag_days']}d ({v['status']})" for k, v in sources.items()}, indent=1))
    print("[wrote]", p)

if __name__ == "__main__":
    main()
