#!/usr/bin/env python3
"""Canonical writer — the ONE process allowed to mutate the canonical store (PRD V2 §30/§31).

    workers stage rows (JSON array / JSONL, scraper shape v1|v2|nightly)  ──►  this writer
      fcntl lock  →  normalize (same SQL as the migration)  →  dedup vs canonical_index
      →  append parquet/source=ebay/year=/month=/ingest_<run>_*.parquet  →  index + ledger + report

Idempotent: ingesting the same staged file twice adds 0 rows (the second run records them as duplicates).
Dedup key: (observation_id, sold_date) — a known item id sold again on another day (multi-quantity BIN) is a
new observation; everything else with a known id is a duplicate re-discovery (logged, not stored).
Never touches the legacy JSON stores. Never writes outside external_store/. Lock: external_store/.writer.lock.

Usage (lane venv):
  .venv_extcomps/bin/python external_engine/canonical_writer.py ingest --in FILE [--source ebay_player_matrix]
        [--lane freshness|backfill|targeted|nightly] [--query-id ID] [--window-start D --window-end D] [--dry-run]
  .venv_extcomps/bin/python external_engine/canonical_writer.py rebuild-index      # (re)build canonical_index from parquet
  .venv_extcomps/bin/python external_engine/canonical_writer.py stats
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import duckdb  # noqa: E402
import canonical_migrate as cm  # noqa: E402
from ledger import Ledger  # noqa: E402

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
WRITER_VERSION = "0.1.0"


class WriterBusy(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CanonicalWriter:
    def __init__(self, store: str | None = None, out: str | None = None, memory_limit: str = "600MB", threads: int = 1,
                 ledger: Ledger | None = None):
        self.store = store or STORE
        self.out = out or os.path.join(self.store, "external_store")
        self.canon_dir = os.path.join(self.out, "parquet", "source=ebay")
        self.db_path = os.path.join(self.out, "canonical_v1.duckdb")
        self.reports = os.path.join(self.out, "reports")
        os.makedirs(self.canon_dir, exist_ok=True); os.makedirs(self.reports, exist_ok=True)
        os.makedirs(os.path.join(self.out, "ledger"), exist_ok=True); os.makedirs(os.path.join(self.out, "duckdb_tmp"), exist_ok=True)
        self.lock_path = os.path.join(self.out, ".writer.lock")
        self._lock = None
        self.con = duckdb.connect(self.db_path)
        self.con.execute(f"SET memory_limit='{memory_limit}'"); self.con.execute(f"SET threads={threads}")
        self.con.execute(f"SET temp_directory='{os.path.join(self.out, 'duckdb_tmp')}'")
        self.ledger = ledger if ledger is not None else Ledger(path=os.path.join(self.out, "ledger", "external_acquisition_ledger.sqlite"))

    # --- lock -----------------------------------------------------------------------------
    def acquire(self) -> None:
        self._lock = open(self.lock_path, "a+")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise WriterBusy(f"another writer holds {self.lock_path}")
        self._lock.seek(0); self._lock.truncate(); self._lock.write(f"{os.getpid()} {_now()}"); self._lock.flush()

    def release(self) -> None:
        if self._lock:
            fcntl.flock(self._lock, fcntl.LOCK_UN); self._lock.close(); self._lock = None

    # --- index ----------------------------------------------------------------------------
    def canonical_rel(self) -> str:
        return f"read_parquet('{self.canon_dir}/**/*.parquet', hive_partitioning=true, union_by_name=true)"

    def ensure_index(self, rebuild: bool = False) -> int:
        exists = self.con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='canonical_index'").fetchone()[0]
        if exists and not rebuild:
            return self.con.execute("SELECT count(*) FROM canonical_index").fetchone()[0]
        has_parquet = any(f.endswith(".parquet") for _, _, fs in os.walk(self.canon_dir) for f in fs)
        self.con.execute("DROP TABLE IF EXISTS canonical_index")
        if has_parquet:
            self.con.execute(f"""CREATE TABLE canonical_index AS
                SELECT observation_id, sold_date, min(first_seen_at) AS first_seen_at
                FROM {self.canonical_rel()} GROUP BY observation_id, sold_date""")
        else:
            self.con.execute("CREATE TABLE canonical_index(observation_id VARCHAR, sold_date DATE, first_seen_at TIMESTAMP)")
        return self.con.execute("SELECT count(*) FROM canonical_index").fetchone()[0]

    # --- ingest ---------------------------------------------------------------------------
    def ingest(self, infile: str, source_label: str | None = None, lane: str = "freshness", query_id: str | None = None,
               subject: str | None = None, window_start: str | None = None, window_end: str | None = None,
               dry_run: bool = False, code_version: str = WRITER_VERSION) -> dict:
        infile = os.path.abspath(infile)
        if not os.path.exists(infile):
            raise FileNotFoundError(infile)
        run_id = self.ledger.start_run("ebay", lane, query_id=query_id, subject=subject, window_start=window_start,
                                       window_end=window_end, code_version=code_version,
                                       extra={"infile": infile, "source_label": source_label, "writer": WRITER_VERSION})
        t0 = time.time()
        try:
            self.acquire()
            n_index = self.ensure_index()
            cols = ", ".join(f"'{k}': '{v}'" for k, v in cm.RAW_COLUMNS.items())
            fmt = "newline_delimited" if infile.endswith(".jsonl") else "array"
            base = os.path.basename(infile)
            stamp_src = f"COALESCE(NULLIF(source,''), '{source_label}')" if source_label else "source"
            now = _now()
            self.con.execute(f"""
                CREATE OR REPLACE TEMP TABLE stage_raw AS
                SELECT * REPLACE ({stamp_src} AS source,
                                  COALESCE(NULLIF(scraped_at,''), '{now}') AS scraped_at,
                                  COALESCE(NULLIF(captured_at,''), '{now}') AS captured_at),
                       '{base}' AS src_file, row_number() OVER () AS src_row
                FROM read_json('{infile}', format='{fmt}', records=true, columns={{{cols}}}, maximum_object_size=33554432)
            """)
            rows_seen = self.con.execute("SELECT count(*) FROM stage_raw").fetchone()[0]
            self.con.execute("CREATE OR REPLACE TEMP TABLE stage_norm AS " + cm.NORMALIZE_SQL.format(src="stage_raw"))
            # dedup within the staged batch first (same item+date twice in one file), then against the index
            self.con.execute("""
                CREATE OR REPLACE TEMP TABLE stage_new AS
                SELECT * EXCLUDE (rn) FROM (
                  SELECT n.*, row_number() OVER (PARTITION BY n.observation_id, n.sold_date ORDER BY n.src_row) AS rn
                  FROM stage_norm n
                  LEFT JOIN canonical_index i ON i.observation_id = n.observation_id
                       AND (i.sold_date IS NOT DISTINCT FROM n.sold_date)
                  WHERE n.source_item_id IS NOT NULL AND n.source_item_id <> '' AND i.observation_id IS NULL
                ) WHERE rn = 1""")
            rows_new = self.con.execute("SELECT count(*) FROM stage_new").fetchone()[0]
            rows_noid = self.con.execute("SELECT count(*) FROM stage_norm WHERE source_item_id IS NULL OR source_item_id=''").fetchone()[0]
            rows_dup = rows_seen - rows_new - rows_noid
            self.ledger.checkpoint(run_id, {"phase": "deduped"}, rows_seen=rows_seen, rows_new=rows_new,
                                   rows_duplicate=rows_dup, rows_rejected=rows_noid)
            written_files = 0
            if rows_new and not dry_run:
                # shape exactly like the migration's canonical rows (single observation → trivial lists)
                self.con.execute(f"""
                    CREATE OR REPLACE TEMP TABLE stage_canon AS
                    SELECT observation_id, source, source_item_id, title, sold_price_usd, 'USD' AS currency,
                           best_offer, sold_date, captured_at, shipping_text, bids_text, condition, grade_company, grade,
                           CASE WHEN grade_company IS NOT NULL THEN 'graded' ELSE 'raw_or_unknown' END AS raw_or_graded,
                           source_url, image_url, subject_query AS primary_subject_query,
                           1::BIGINT AS n_observations, captured_at AS first_seen_at, captured_at AS last_seen_at,
                           [discovery_source] AS discovery_sources,
                           CASE WHEN subject_query IS NULL THEN [] ELSE [subject_query] END AS queries_seen,
                           CASE WHEN legacy_comp_id IS NULL THEN [] ELSE [legacy_comp_id] END AS legacy_comp_ids,
                           FALSE AS price_conflict, FALSE AS date_conflict, FALSE AS image_conflict,
                           src_file AS chosen_src_file, src_row AS chosen_src_row,
                           '{cm.NORMALIZER_VERSION}' AS normalizer_version,
                           COALESCE(year(sold_date), 0) AS year, COALESCE(month(sold_date), 0) AS month
                    FROM stage_new""")
                tag = f"ingest_{run_id.replace(':', '')}"
                self.con.execute(f"""
                    COPY stage_canon TO '{self.canon_dir}'
                    (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (year, month), APPEND true,
                     FILENAME_PATTERN '{tag}_{{uuid}}')""")
                self.con.execute("INSERT INTO canonical_index SELECT observation_id, sold_date, captured_at FROM stage_new")
                written_files = sum(1 for _, _, fs in os.walk(self.canon_dir) for f in fs if f.startswith(tag))
            self.ledger.complete(run_id, rows_seen=rows_seen, rows_new=(0 if dry_run else rows_new),
                                 rows_duplicate=rows_dup, rows_rejected=rows_noid)
            report = {"run_id": run_id, "infile": infile, "source_label": source_label, "lane": lane, "query_id": query_id,
                      "window": [window_start, window_end], "rows_seen": rows_seen, "rows_new": rows_new,
                      "rows_duplicate": rows_dup, "rows_rejected_no_id": rows_noid, "index_before": n_index,
                      "written_files": written_files, "dry_run": dry_run, "seconds": round(time.time() - t0, 2),
                      "at": _now(), "writer_version": WRITER_VERSION}
            with open(os.path.join(self.reports, f"ingest_{run_id}.json"), "w") as f:
                json.dump(report, f, indent=1, default=str)
            return report
        except WriterBusy:
            self.ledger.fail(run_id, "writer busy", retry=True)
            raise
        except Exception as e:
            self.ledger.fail(run_id, f"{type(e).__name__}: {e}", retry=True)
            raise
        finally:
            self.release()

    def stats(self) -> dict:
        n_idx = self.ensure_index()
        n_files = sum(1 for _, _, fs in os.walk(self.canon_dir) for f in fs if f.endswith(".parquet"))
        return {"canonical_index_rows": n_idx, "parquet_files": n_files, "canon_dir": self.canon_dir,
                "ledger": self.ledger.summary(24)}

    def close(self) -> None:
        self.release(); self.con.close(); self.ledger.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest"); i.add_argument("--in", dest="infile", required=True); i.add_argument("--source", default=None)
    i.add_argument("--lane", default="freshness"); i.add_argument("--query-id", default=None); i.add_argument("--subject", default=None)
    i.add_argument("--window-start", default=None); i.add_argument("--window-end", default=None); i.add_argument("--dry-run", action="store_true")
    sub.add_parser("rebuild-index"); sub.add_parser("stats")
    for p in (i,):
        pass
    ap.add_argument("--store", default=None); ap.add_argument("--out", default=None)
    a = ap.parse_args()
    w = CanonicalWriter(store=a.store, out=a.out)
    try:
        if a.cmd == "ingest":
            print(json.dumps(w.ingest(a.infile, a.source, a.lane, a.query_id, a.subject, a.window_start, a.window_end, a.dry_run), indent=1, default=str))
        elif a.cmd == "rebuild-index":
            w.acquire(); print("canonical_index rows:", w.ensure_index(rebuild=True))
        else:
            print(json.dumps(w.stats(), indent=1, default=str))
    except WriterBusy as e:
        print("WRITER BUSY:", e); sys.exit(3)
    finally:
        w.close()


if __name__ == "__main__":
    main()
