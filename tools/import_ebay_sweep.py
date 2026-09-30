#!/usr/bin/env python3
"""import_ebay_sweep.py -- land tools/ebay_signed_in_sweep.py output in Neon external_transactions (source_code 'ebay').

Same insert as the proven VeeFriends importer (tools/import_ebay_veefriends_pilot.py: batch of 500, ON CONFLICT DO NOTHING
with no conflict target, so the item-id, canonical-url and title+date+price guards all hold; a non-session error replays
the batch row by row). Best Offer rows are stored with best_offer=true -- context only, never a confirmed price.

SPORTS IS REFUSED until an SCP guard exists: SportsCardsPro rows ARE eBay sales (raw->>'ledger_anchor' = ebay-<id>) and
neither unique index spans the two source codes, so a direct eBay sports row would double-count an SCP row. Pokemon has no
SCP rows.

  python3 tools/import_ebay_sweep.py --segments pokemon --once --max-rows 200     # small first import
  python3 tools/import_ebay_sweep.py --segments pokemon --follow                  # every 10 min, byte offsets remembered
"""
import argparse, glob, json, os, sys, time
from datetime import datetime
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from mazi_db.scripts.scp_scraper import ExternalRow  # noqa: E402
from mazi_db.scripts.trusted_enrichment_spine import dsn_for_prod, insert_external_transaction  # noqa: E402
from bridge_scp_broad_to_neon import SESSION_ERRORS  # noqa: E402
from import_ebay_veefriends_pilot import insert_batch  # noqa: E402

OUT = "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_signed_in"
STATE = os.path.expanduser("~/mazi_ebay_sweep/import_state.json")
ALLOWED = {"pokemon"}


def to_row(r):
    try:
        price = Decimal(str(r.get("sold_price")))
    except (InvalidOperation, ValueError):
        return None
    if not r.get("item_id") or price <= 0 or not r.get("sold_date") or not r.get("title"):
        return None
    raw = {k: r.get(k) for k in ("shipping", "condition", "bids", "segment", "shard", "page", "scraped_at", "source",
                                "image_url")}    # top-level too, not only in raw.trusted_enrichment (2026-09-29)
    raw["ebay_category"] = r.get("ebay_category")
    return ExternalRow(title=r["title"], sold_price=price, sold_date=datetime.strptime(r["sold_date"], "%Y-%m-%d").date(),
                       source_item_id=str(r["item_id"]), source_url=r.get("url") or "", image_url=r.get("image_url") or "",
                       best_offer=bool(r.get("best_offer")), query_variant="ebay_signed_in_sweep_v1",
                       query_text=r.get("segment") or "", price_range="", raw=raw)


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(st):
    tmp = STATE + ".tmp"; json.dump(st, open(tmp, "w"), indent=1); os.replace(tmp, STATE)


def cycle(segments, max_rows=None):
    st = load_state()
    files = sorted(f for seg in segments for f in glob.glob(os.path.join(OUT, seg, "sold_*.jsonl")))
    pending = []
    for f in files:
        size = os.path.getsize(f)
        if size > st.get(f, 0):
            pending.append((f, st.get(f, 0), size))
    if not pending:
        return 0, 0
    conn = psycopg.connect(dsn_for_prod(), row_factory=dict_row, keepalives=1, keepalives_idle=30)
    cur = conn.cursor()
    rid = cur.execute("""INSERT INTO public.comp_import_runs (lane, source_code, input_uri, target_db, dry_run, status, params)
                         VALUES ('nightly_delta','ebay',%s,'prod',false,'started',%s) RETURNING id""",
                      (OUT, Jsonb({"engine": "ebay_signed_in_sweep_v1", "segments": segments, "approved_by": "andy 2026-09-29"}))).fetchone()["id"]
    conn.commit()
    offered = new = fallback = 0
    try:
        for f, start, end in pending:
            with open(f, "rb") as fh:
                fh.seek(start)
                chunk = fh.read(end - start)
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                continue
            lines = chunk[:last_nl + 1].decode("utf-8", "replace").splitlines()
            rows = [x for x in (to_row(json.loads(l)) for l in lines if l.strip()) if x]
            if max_rows:
                rows = rows[:max_rows]
            for i in range(0, len(rows), 500):
                part = rows[i:i + 500]
                try:
                    with conn.transaction():
                        new += insert_batch(cur, part, rid)
                except SESSION_ERRORS:
                    raise
                except Exception as e:
                    print(f"[batch-fallback] {os.path.basename(f)} rows {i}-{i + len(part)}: {type(e).__name__}: {e}", flush=True)
                    for r in part:
                        with conn.transaction():
                            insert_external_transaction(cur, r, rid, None)
                        fallback += 1
            offered += len(rows)
            if not max_rows:
                st[f] = start + last_nl + 1
                save_state(st)
            if max_rows:
                break
        cur.execute("UPDATE public.comp_import_runs SET status='completed', params = params || %s WHERE id=%s",
                    (Jsonb({"rows_offered": offered, "rows_new": new, "fallback_rows": fallback}), rid))
        conn.commit()
    finally:
        conn.close()
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] run {rid}: offered {offered:,} | NEW {new:,} | fallback {fallback}", flush=True)
    return offered, new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--segments", type=lambda s: s.split(","), default=["pokemon"])
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--max-rows", type=int, help="first test: import only this many rows (offsets not advanced)")
    ap.add_argument("--every", type=int, default=600)
    a = ap.parse_args()
    bad = set(a.segments) - ALLOWED
    if bad:
        sys.exit(f"refusing {sorted(bad)}: sports needs the SCP duplicate guard first (see docstring)")
    while True:
        try:
            cycle(a.segments, a.max_rows)
        except SESSION_ERRORS as e:
            print(f"session error, retrying next cycle: {type(e).__name__}: {e}", flush=True)
        if not a.follow:
            return 0
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
