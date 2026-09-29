#!/usr/bin/env python3
"""import_ebay_veefriends_pilot.py -- land the signed-in VeeFriends eBay pull in Neon (Andy: GO, 2026-09-27).

Same values as the proven insert_external_transaction: source_code 'ebay', source_item_id = eBay item id,
canonical_source_url = https://www.ebay.com/itm/<id>, normalized_title = norm_title(title), raw + trusted_enrichment.
One multi-row INSERT per 500 rows with ON CONFLICT DO NOTHING and NO conflict target, so all three duplicate guards
the per-row path checks (item id, canonical url, title+date+price) still hold. A non-session error replays that
batch through insert_external_transaction. --parity N writes N rows both ways inside ROLLED-BACK transactions and
compares every column first.

  python3 tools/import_ebay_veefriends_pilot.py --file <dedup.jsonl> --parity 200
  python3 tools/import_ebay_veefriends_pilot.py --file <dedup.jsonl> --commit
"""
import argparse, json, os, sys, time
from datetime import datetime
from decimal import Decimal, InvalidOperation
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from mazi_db.scripts.scp_scraper import ExternalRow  # noqa: E402
from mazi_db.scripts.trusted_enrichment_spine import (  # noqa: E402
    dsn_for_prod, insert_external_transaction, norm_title, normalized_source_url, serialize_external_row,
)
from bridge_scp_broad_to_neon import SESSION_ERRORS  # noqa: E402

COLS = ("source_item_id", "source_url", "canonical_url", "title", "normalized", "sold_price", "sold_date",
        "best_offer", "run_id", "raw")


def to_row(r):
    try:
        price = Decimal(str(r.get("sold_price")))
    except (InvalidOperation, ValueError):
        return None
    if not r.get("item_id") or price <= 0:
        return None
    raw = {k: r.get(k) for k in ("shipping", "condition", "bids", "query", "page", "scraped_at", "source")}
    raw.update({"ebay_category": "183050", "pilot": "veefriends_signed_in_2026-09-27"})
    return ExternalRow(title=r["title"], sold_price=price, sold_date=datetime.strptime(r["sold_date"], "%Y-%m-%d").date(),
                       source_item_id=str(r["item_id"]), source_url=r.get("url") or "", image_url=r.get("image_url") or "",
                       best_offer=bool(r.get("best_offer")), query_variant="ebay_vf_pilot_v1", query_text="veefriends",
                       price_range="", raw=raw)


def params(row, run_id):
    return {"source_item_id": row.source_item_id, "source_url": row.source_url,
            "canonical_url": normalized_source_url(row.source_url), "title": row.title, "normalized": norm_title(row.title),
            "sold_price": row.sold_price, "sold_date": row.sold_date, "best_offer": row.best_offer, "run_id": run_id,
            "raw": Jsonb({**row.raw, "trusted_enrichment": serialize_external_row(row)})}


def insert_batch(cur, rows, run_id) -> int:
    one = "('ebay', %s, %s, %s, %s, %s, %s, %s, 'USD', %s, %s, %s, NOW())"
    flat = []
    for r in rows:
        p = params(r, run_id); flat.extend(p[k] for k in COLS)
    cur.execute("""INSERT INTO public.external_transactions (
                     source_code, source_item_id, source_url, canonical_source_url, title, normalized_title, sold_price,
                     sold_date, currency, best_offer, imported_from_run_id, raw, scraped_at)
                   VALUES """ + ", ".join([one] * len(rows)) + " ON CONFLICT DO NOTHING RETURNING id", flat)
    return len(cur.fetchall())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--parity", type=int, default=0)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--batch", type=int, default=500)
    a = ap.parse_args()
    raw_rows = [json.loads(l) for l in open(os.path.expanduser(a.file))]
    rows = [x for x in (to_row(r) for r in raw_rows) if x]
    print(f"{len(raw_rows):,} pulled rows -> {len(rows):,} importable (dropped {len(raw_rows) - len(rows)} without item id/price)")
    conn = psycopg.connect(dsn_for_prod(), row_factory=dict_row, keepalives=1, keepalives_idle=30)
    cur = conn.cursor()
    if a.parity:
        ids = [r.source_item_id for r in rows]
        have = {x["source_item_id"] for x in cur.execute(
            "SELECT source_item_id FROM public.external_transactions WHERE source_code='ebay' AND source_item_id = ANY(%s)",
            (ids,)).fetchall()}
        conn.rollback()
        print(f"already in Neon by item id: {len(have):,}")
        sample = [r for r in rows if r.source_item_id not in have][:a.parity]
        sids = [r.source_item_id for r in sample]
        SKIP = {"id", "created_at", "updated_at", "scraped_at", "imported_from_run_id"}
        out = []
        for label, fn in (("per-row", lambda rid: [insert_external_transaction(cur, r, rid, None) for r in sample]),
                          ("batch", lambda rid: insert_batch(cur, sample, rid))):
            with conn.transaction():
                rid = cur.execute("""INSERT INTO public.comp_import_runs (lane, source_code, input_uri, target_db, dry_run, status, params)
                                     VALUES ('historical_backfill','ebay','parity_gate','prod',true,'started',%s) RETURNING id""",
                                  (Jsonb({"engine": "parity_gate_rollback"}),)).fetchone()["id"]
                t = time.time(); fn(rid); secs = time.time() - t
                got = {g["source_item_id"]: g for g in cur.execute(
                    "SELECT * FROM public.external_transactions WHERE source_code='ebay' AND source_item_id = ANY(%s)", (sids,)).fetchall()}
                print(f"{label}: {len(got)} rows in {secs:.1f}s (rolled back)")
                out.append(got)
                raise psycopg.Rollback()
        mism = [s for s in sids if {k: v for k, v in (out[0].get(s) or {}).items() if k not in SKIP}
                != {k: v for k, v in (out[1].get(s) or {}).items() if k not in SKIP}]
        left = cur.execute("SELECT count(*) n FROM public.external_transactions WHERE source_code='ebay' AND source_item_id = ANY(%s)",
                           (sids,)).fetchone()["n"]
        conn.rollback()
        print(f"PARITY: {len(sids)} rows compared, mismatches {len(mism)}, persisted after rollback {left}")
        return 0 if not mism and left == 0 else 1
    if not a.commit:
        print("dry run: pass --parity N or --commit"); return 0
    rid = cur.execute("""INSERT INTO public.comp_import_runs (lane, source_code, input_uri, target_db, dry_run, status, params)
                         VALUES ('historical_backfill','ebay',%s,'prod',false,'started',%s) RETURNING id""",
                      (os.path.expanduser(a.file), Jsonb({"engine": "ebay_vf_pilot_v1", "approved_by": "andy 2026-09-27"}))).fetchone()["id"]
    conn.commit()
    new = fallback_rows = 0
    for i in range(0, len(rows), a.batch):
        chunk = rows[i:i + a.batch]
        try:
            with conn.transaction():
                new += insert_batch(cur, chunk, rid)
        except SESSION_ERRORS:
            raise
        except Exception as e:
            print(f"[batch-fallback] rows {i}-{i + len(chunk)}: {type(e).__name__}: {e}", flush=True)
            for r in chunk:
                with conn.transaction():
                    insert_external_transaction(cur, r, rid, None)
                fallback_rows += 1
    cur.execute("UPDATE public.comp_import_runs SET status='completed', params = params || %s WHERE id=%s",
                (Jsonb({"rows_offered": len(rows), "rows_new": new, "fallback_rows": fallback_rows}), rid))
    conn.commit()
    print(f"run {rid}: offered {len(rows):,} | NEW {new:,} | already present {len(rows) - new - fallback_rows:,} | via fallback {fallback_rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
