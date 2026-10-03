#!/usr/bin/env python3
"""
bridge_scp_broad_to_neon.py — roll broad-SCP local JSONL into Neon prod.
========================================================================
Drains scp_broad_comps.jsonl (produced by scp_broad_scrub.py) into
public.external_transactions via the PROVEN idempotent insert_scp_transaction
(source_code='sportscardspro', ON CONFLICT (source_code, source_item_id) DO
UPDATE). target_id=None -> the cross-source eBay guard safely no-ops (our window
is <= 2025-12-31 where the eBay corpus is negligible; both are legit sources).

Byte-offset watermark (state file) so re-runs only process new appended lines;
ON CONFLICT makes even a re-read harmless. --loop keeps draining alongside the
live scrape (rolling bridge). Reference-only comps; NEVER Trusted/Mazified.

Usage:
  python3 bridge_scp_broad_to_neon.py --jsonl ~/mazi_scp_broad/scp_broad_comps.jsonl --once --max 2000
  python3 bridge_scp_broad_to_neon.py --jsonl ~/mazi_scp_broad/scp_broad_comps.jsonl --loop --interval 900
  python3 bridge_scp_broad_to_neon.py --jsonl ... --once --max 5000 --batch-insert

--batch-insert (or env MAZI_SCP_BRIDGE_BATCH=1; default OFF) writes each batch as one
multi-row INSERT instead of ~5 Neon round trips per row -- see drain_once_batched.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(os.environ.get("MAZI_REPO_ROOT", os.path.expanduser("~/whatnot-sniper")))
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from mazi_db.scripts.scp_scraper import ExternalRow  # noqa: E402
from mazi_db.scripts.trusted_enrichment_spine import (  # noqa: E402
    SCP_SOURCE_CODE, dsn_for_prod, insert_scp_transaction, norm_title, serialize_external_row,
)

# NOT data errors -- the SESSION is unusable. Counting these as `skipped` is exactly
# how 34,000 rows were silently lost; they must abort the drain with the watermark held.
SESSION_ERRORS = (psycopg.errors.ReadOnlySqlTransaction,
                  psycopg.errors.AdminShutdown,
                  psycopg.errors.CannotConnectNow,
                  psycopg.OperationalError)


def row_from_jsonl(d: dict) -> ExternalRow:
    # catalog_id (PriceCharting/SCP global card id) is written at the jsonl TOP LEVEL
    # by scp_broad_scrub, while raw carries the per-sale payload. Merge it in: it is
    # the global identifier the front end joins on, and external_transactions has no
    # card_id column, so raw is the only place it can live today. slug is kept
    # alongside it as the human-readable fallback key.
    raw = dict(d.get("raw") or {})
    if d.get("catalog_id") and not raw.get("catalog_id"):
        raw["catalog_id"] = str(d["catalog_id"])
    return ExternalRow(
        title=d["title"],
        sold_price=Decimal(str(d["sold_price"])),
        sold_date=datetime.strptime(d["sold_date"], "%Y-%m-%d").date(),
        source_item_id=d["source_item_id"],
        source_url=d.get("source_url", ""),
        image_url=d.get("image_url", ""),
        best_offer=False,
        query_variant="scp_broad_ledger",
        query_text=d.get("slug", ""),
        price_range="scp_history",
        raw=raw,
    )


def create_run(conn, jsonl_path: Path) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO public.comp_import_runs
              (lane, source_code, input_uri, target_db, dry_run, status, params)
            VALUES ('historical_backfill', 'sportscardspro', %s, 'prod', false, 'started', %s)
            RETURNING id
            """,
            (str(jsonl_path), Jsonb({"engine": "scp_broad_scrub"})),
        )
        rid = int(cur.fetchone()["id"])
    conn.commit()
    return rid


def load_offset(state: Path) -> int:
    if state.exists():
        try:
            return int(json.loads(state.read_text()).get("offset", 0))
        except Exception:
            pass
    return 0


def save_offset(state: Path, offset: int, inserted: int, skipped: int) -> None:
    tmp = state.with_suffix(".tmp")
    tmp.write_text(json.dumps({"offset": offset, "inserted": inserted, "skipped": skipped}))
    tmp.replace(state)


def _safe_commit_and_save(conn, state, offset, inserted, skipped) -> bool:
    """Commit, then advance the watermark ONLY if the transaction really committed.

    conn.commit() on an aborted transaction returns NORMALLY (no exception), so the
    caller cannot tell success from silent discard without inspecting the status.
    Returns False when the watermark was deliberately held back."""
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
        print("[hold-watermark] transaction INERROR — rolling back, NOT advancing offset", flush=True)
        conn.rollback()
        return False
    conn.commit()
    save_offset(state, offset, inserted, skipped)
    return True


def drain_once(conn, jsonl: Path, state: Path, run_id: int, batch_size: int, max_rows: int | None) -> dict:
    """Process new complete lines from the byte-offset watermark. Returns counts."""
    offset = load_offset(state)
    inserted = skipped = processed = 0
    batch_new_offset = offset
    if not jsonl.exists():
        return {"processed": 0, "inserted": 0, "skipped": 0, "offset": offset}
    cur = conn.cursor(row_factory=dict_row)
    with open(jsonl, "rb") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line:
                break
            if not line.endswith(b"\n"):  # partial trailing line (scrape mid-write) -> stop, don't consume
                break
            try:
                d = json.loads(line.decode("utf-8"))
                row = row_from_jsonl(d)
                # SAVEPOINT per row: without it, ONE failing insert aborts the whole
                # transaction and every later row in the batch fails as collateral.
                with conn.transaction():
                    tid = insert_scp_transaction(cur, row, run_id, None)
                if tid is None:
                    skipped += 1  # cross-source dup guard (rare; target_id=None => ~never)
                else:
                    inserted += 1
            except SESSION_ERRORS as e:
                # NOT a data error -- the SESSION is unusable. Counting these as
                # `skipped` is exactly how 34,000 rows were silently lost. Abort the
                # drain WITHOUT advancing the watermark so they are re-read later.
                print(f"[abort-drain] session-level failure, watermark held: {e}", flush=True)
                raise
            except Exception as e:
                skipped += 1
                print(f"[skip] {e}", flush=True)
            processed += 1
            batch_new_offset = fh.tell()
            if processed % batch_size == 0:
                if not _safe_commit_and_save(conn, state, batch_new_offset, inserted, skipped):
                    break
            if max_rows is not None and processed >= max_rows:
                break
    _safe_commit_and_save(conn, state, batch_new_offset, inserted, skipped)
    cur.close()
    return {"processed": processed, "inserted": inserted, "skipped": skipped, "offset": batch_new_offset}


# ── Batched fast path (--batch-insert / MAZI_SCP_BRIDGE_BATCH=1, default OFF) ─────────
# drain_once pays ~5 Neon round trips PER ROW (BEGIN, guard SELECT, idempotency SELECT,
# INSERT, COMMIT) at ~76 ms each from the Mini => ~3 rows/s, slower than the scraper
# appends, so the lag grows. This path writes a batch as ONE multi-row INSERT.
#   * Same column values: scp_insert_params uses the same expressions as
#     insert_scp_transaction step (3); tools/test_bridge_batch.py pins the parity.
#   * No cross-source guard: the bridge always passes target_id=None, where the guard's
#     `c.target_id = NULL` provably matches nothing. Deep-scrub (a real target_id) calls
#     insert_scp_transaction directly and never comes through here.
#   * DO NOTHING, not DO UPDATE: the per-row path returns early from its idempotency
#     SELECT on an existing row, so it never updates one either. RETURNING then yields
#     only genuinely new rows, so `inserted` means inserted and `existing` is re-reads.
#   * ON CONFLICT names one index; url_uidx / title_date_price_uidx can still raise
#     mid-statement. Any non-session error replays THAT batch row by row through
#     insert_scp_transaction, so one poison row cannot lose the other 499.

_SCP_COLS = ("source_code", "source_item_id", "source_url", "canonical_url", "title",
             "normalized", "sold_price", "sold_date", "best_offer", "run_id", "raw")


def scp_insert_params(row: ExternalRow, run_id: int) -> dict:
    """The values insert_scp_transaction binds for its INSERT, for one row."""
    return {
        "source_code": SCP_SOURCE_CODE,
        "source_item_id": row.source_item_id,
        "source_url": row.source_url,
        "canonical_url": None,
        "title": row.title,
        "normalized": norm_title(
            f"{row.raw.get('scp_original_title') or row.title} {row.source_item_id}"),
        "sold_price": row.sold_price,
        "sold_date": row.sold_date,
        "best_offer": row.best_offer,
        "run_id": run_id,
        "raw": Jsonb({**row.raw, "trusted_enrichment": serialize_external_row(row)}),
    }


def insert_scp_batch(cur, rows: list, run_id: int) -> int:
    """Insert rows with one statement. Returns how many were genuinely new."""
    if not rows:
        return 0
    one = "(%s, %s, %s, %s, %s, %s, %s, %s, 'USD', %s, %s, %s, NOW())"
    params = []
    for row in rows:
        p = scp_insert_params(row, run_id)
        params.extend(p[k] for k in _SCP_COLS)
    cur.execute(
        """
        INSERT INTO public.external_transactions (
          source_code, source_item_id, source_url, canonical_source_url, title,
          normalized_title, sold_price, sold_date, currency, best_offer,
          imported_from_run_id, raw, scraped_at
        ) VALUES """ + ", ".join([one] * len(rows)) + """
        ON CONFLICT (source_code, source_item_id) WHERE source_item_id IS NOT NULL
        DO NOTHING
        RETURNING id
        """,
        params,
    )
    return len(cur.fetchall())


def _write_batch(conn, cur, rows: list, run_id: int) -> tuple:
    """Write one batch; returns (inserted, existing, skipped). Raises SESSION_ERRORS."""
    try:
        with conn.transaction():
            new = insert_scp_batch(cur, rows, run_id)
        return new, len(rows) - new, 0
    except psycopg.errors.QueryCanceled as e:
        # statement_timeout is an OperationalError subclass, but the session is fine
        # once the block rolls back -- replay instead of re-reading this batch forever.
        why = e
    except SESSION_ERRORS:
        raise
    except Exception as e:
        why = e
    print(f"[batch-fallback] {type(why).__name__}: {why} -- replaying {len(rows)} rows one by one",
          flush=True)
    inserted = skipped = 0
    for row in rows:
        try:
            with conn.transaction():
                tid = insert_scp_transaction(cur, row, run_id, None)
        except SESSION_ERRORS as e:
            print(f"[abort-drain] session-level failure, watermark held: {e}", flush=True)
            raise
        except Exception as e:
            skipped += 1
            print(f"[skip] {e}", flush=True)
            continue
        if tid is None:
            skipped += 1
        else:
            inserted += 1  # legacy meaning here: the row is present (new or re-read)
    return inserted, 0, skipped


def drain_once_batched(conn, jsonl: Path, state: Path, run_id: int, batch_size: int,
                       max_rows: int | None) -> dict:
    """drain_once, one INSERT per batch. The watermark only moves past a batch after it
    has committed, and never past a held-back one."""
    offset = load_offset(state)
    inserted = existing = skipped = processed = 0
    end = offset          # byte offset just past the last line read
    pending = []
    if not jsonl.exists():
        return {"processed": 0, "inserted": 0, "existing": 0, "skipped": 0, "offset": offset}
    cur = conn.cursor(row_factory=dict_row)

    def flush() -> bool:
        nonlocal inserted, existing, skipped, offset
        if pending:
            n_new, n_old, n_skip = _write_batch(conn, cur, pending, run_id)
            inserted += n_new
            existing += n_old
            skipped += n_skip
            pending.clear()
        if not _safe_commit_and_save(conn, state, end, inserted, skipped):
            return False
        offset = end
        return True

    held = False
    with open(jsonl, "rb") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line or not line.endswith(b"\n"):  # EOF, or scrape mid-write
                break
            try:
                pending.append(row_from_jsonl(json.loads(line.decode("utf-8"))))
            except Exception as e:
                skipped += 1
                print(f"[skip] {e}", flush=True)
            processed += 1
            end = fh.tell()
            if processed % batch_size == 0 and not flush():
                held = True
                break
            if max_rows is not None and processed >= max_rows:
                break
    if not held:
        flush()
    cur.close()
    return {"processed": processed, "inserted": inserted, "existing": existing,
            "skipped": skipped, "offset": offset}


def connect_db():
    """Fresh prod connection with TCP keepalives. Root cause of the 436 observed
    'connection is lost' crashes: the --loop sleep (600s) leaves the session idle
    long enough for Neon to reap it, so the NEXT drain's first statement hits a
    dead socket. Keepalives hold it open; with_reconnect() recovers if it still drops."""
    conn = psycopg.connect(
        dsn_for_prod(), row_factory=dict_row,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
    )
    conn.autocommit = False
    conn.read_only = False    # BEGIN READ WRITE: immune to a read-only default leaked through the Neon pooler (2026-10-02)
    return conn


def with_reconnect(conn, fn, *a, **kw):
    """Run fn(conn, ...); on a dropped connection reconnect ONCE and retry.
    Safe because every drain resumes from the persisted byte-offset watermark and
    insert_scp_transaction is idempotent (ON CONFLICT DO NOTHING)."""
    try:
        return conn, fn(conn, *a, **kw)
    except psycopg.OperationalError as e:
        print(f"[reconnect] {e}", flush=True)
        try:
            conn.close()
        except Exception:
            pass
        time.sleep(5)
        conn = connect_db()
        return conn, fn(conn, *a, **kw)


COUNT_EVERY = 12          # cycles between the (expensive) corpus count
_cycle = 0


def scp_count(conn):
    """Best-effort corpus count for the progress line. Returns None when the count
    is skipped or fails.

    This is a READOUT, not work: at 3M+ matching rows the count exceeds Neon's
    statement_timeout and raises QueryCanceled, which is NOT an OperationalError
    subclass -- so with_reconnect never caught it and a cosmetic query killed the
    bridge outright. Never let progress reporting be able to stop the drain."""
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET LOCAL statement_timeout = '25s'")
            cur.execute(
                "SELECT count(*) AS n FROM public.external_transactions "
                "WHERE source_code='sportscardspro'")
            return int(cur.fetchone()["n"])
    except Exception as e:
        print(f"[count skipped] {type(e).__name__}", flush=True)
        try:
            conn.rollback()      # clear the aborted transaction so the next drain works
        except Exception:
            pass
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--state", default=None, help="watermark file (default <jsonl>.bridge_state.json)")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--max", type=int, default=None, help="cap rows this invocation (bounded first pass)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=900, help="--loop sleep between drains (s)")
    ap.add_argument("--batch-insert", action="store_true",
                    default=os.environ.get("MAZI_SCP_BRIDGE_BATCH") == "1",
                    help="one multi-row INSERT per batch (default off; env MAZI_SCP_BRIDGE_BATCH=1)")
    args = ap.parse_args()

    jsonl = Path(args.jsonl)
    state = Path(args.state) if args.state else jsonl.with_suffix(".bridge_state.json")

    drain = drain_once_batched if args.batch_insert else drain_once
    conn = connect_db()
    try:
        run_id = create_run(conn, jsonl)
        before = scp_count(conn)   # may be None; purely informational
        print(f"[bridge] run_id={run_id}  scp_before={before}  state={state}  offset={load_offset(state)}  "
              f"mode={'batch' if args.batch_insert else 'per-row'}", flush=True)
        while True:
            t0 = time.time()
            conn, r = with_reconnect(conn, drain, jsonl, state, run_id, args.batch_size, args.max)
            secs = time.time() - t0
            global _cycle
            _cycle += 1
            after = None
            if _cycle % COUNT_EVERY == 1:
                conn, after = with_reconnect(conn, scp_count)
            delta = f" scp_now={after} (+{after - before})" if (after is not None and before is not None) else ""
            existing = f" existing={r['existing']}" if "existing" in r else ""
            print(f"[bridge] processed={r['processed']} inserted={r['inserted']}{existing} "
                  f"skipped={r['skipped']}{delta} offset={r['offset']} "
                  f"secs={secs:.1f} rate={r['processed'] / max(secs, 1e-6):.1f}/s", flush=True)
            if after is not None:
                before = after
            if not args.loop or args.max is not None:
                break
            time.sleep(args.interval)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
