#!/usr/bin/env python3
"""Canonical store migration prototype + benchmark (PRD V2 §7/§8/§14) — NON-DESTRUCTIVE.

JSON stores (never modified)  ──►  raw Parquet (immutable copy per source file)
                                        │
                                        ▼   normalize both schema variants (v1 sold_price / v2 price)
                              external_market_canonical_v1  (one row per EBAY:<item_id>)
                                        │   discovery_sources[], queries_seen[], comp_ids[], first/last seen,
                                        │   n_observations, price/date/image conflict flags
                                        ▼
                  parquet/source=ebay/year=YYYY/month=MM/*.parquet  +  canonical_v1.duckdb (views)  +  report

Runs inside a DuckDB memory cap (default 1200 MB, spills to temp on the 6 TB volume) so it is safe
next to the capture fleet. Produces the dedup audit the PRD asks for: duplicates_removed, conflicts.

Usage (lane venv):
  .venv_extcomps/bin/python external_engine/canonical_migrate.py [--store DIR] [--out DIR]
        [--memory-limit 1200MB] [--threads 2] [--files ebay_comps.json,...] [--skip-raw-if-exists]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import duckdb

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

DEFAULT_STORE = "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store"
STORE_FILES = ["ebay_comps.json", "player_comps.json", "raw_player_comps.json"]

# Union of keys across both schema variants — everything read as VARCHAR, typed in SQL.
RAW_COLUMNS = {
    "title": "VARCHAR", "sold_price": "VARCHAR", "sold_date": "VARCHAR", "condition": "VARCHAR",
    "bids": "VARCHAR", "url": "VARCHAR", "image_url": "VARCHAR", "source": "VARCHAR",
    "comp_id": "VARCHAR", "scraped_at": "VARCHAR", "best_offer": "VARCHAR",
    "player_query": "VARCHAR", "grader": "VARCHAR", "grade": "VARCHAR",
    "price": "VARCHAR", "price_text": "VARCHAR", "sold_date_raw": "VARCHAR", "shipping": "VARCHAR",
    "item_id": "VARCHAR", "query_suffix": "VARCHAR", "source_query": "VARCHAR",
    "price_range": "VARCHAR", "query_label": "VARCHAR", "capture_date": "VARCHAR",
    "captured_at": "VARCHAR",
}

NORMALIZE_SQL = r"""
SELECT
  'ebay'                                                             AS source,
  COALESCE(NULLIF(item_id,''), regexp_extract(url, '/itm/(\d+)', 1)) AS source_item_id,
  'EBAY:' || COALESCE(NULLIF(item_id,''), regexp_extract(url, '/itm/(\d+)', 1)) AS observation_id,
  title,
  TRY_CAST(regexp_extract(regexp_replace(COALESCE(NULLIF(sold_price,''), price, ''), '[$,\s]', '', 'g'),
                          '-?[0-9]+(\.[0-9]+)?', 0) AS DOUBLE)                                        AS sold_price_usd,
  CASE lower(COALESCE(best_offer,'')) WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE ELSE NULL END AS best_offer,
  COALESCE(
    TRY_CAST(TRY_STRPTIME(regexp_replace(COALESCE(sold_date,''), '^(Sold|Ended)\s+', ''), '%b %d, %Y') AS DATE),
    TRY_CAST(TRY_STRPTIME(regexp_replace(COALESCE(sold_date,''), '^(Sold|Ended)\s+', ''), '%B %d, %Y') AS DATE),
    TRY_CAST(substr(COALESCE(sold_date,''), 1, 10) AS DATE)
  )                                                                  AS sold_date,
  TRY_CAST(scraped_at AS TIMESTAMP)                                  AS captured_at,
  NULLIF(shipping,'')                                                AS shipping_text,
  NULLIF(bids,'')                                                    AS bids_text,
  NULLIF(condition,'')                                               AS condition,
  NULLIF(grader,'')                                                  AS grade_company,
  NULLIF(grade,'')                                                   AS grade,
  url                                                                AS source_url,
  image_url,
  COALESCE(NULLIF(source,''), CASE WHEN source_query IS NOT NULL THEN 'ebay_v2_priority' ELSE 'ebay_unknown' END,
           'ebay_unknown')                                           AS discovery_source,
  COALESCE(NULLIF(player_query,''), NULLIF(source_query,''),
           NULLIF(url_decode(regexp_extract(url, '[?&]_skw=([^&]*)', 1)),''))     AS subject_query,
  NULLIF(comp_id,'')                                                 AS legacy_comp_id,
  src_file,
  src_row
FROM {src}
"""

# Stage 1 — per-id index in ONE narrow hash aggregate (3.25M groups): counts, first/last seen, and the
# winning row picked via min_by over a composite key (graded first, priced first, earliest capture, then
# stable by src_file/src_row) — no sort of wide rows, single build side for the final join.
IDX_SQL = """
    SELECT observation_id,
      count(*)          AS n_observations,
      min(captured_at)  AS first_seen_at,
      max(captured_at)  AS last_seen_at,
      min_by(struct_pack(src_file := src_file, src_row := src_row),
             struct_pack(g := (grade_company IS NULL), p := (sold_price_usd IS NULL),
                         c := COALESCE(captured_at, TIMESTAMP '9999-12-31 00:00:00'),
                         f := src_file, r := src_row)) AS pick
    FROM {normalized} WHERE source_item_id IS NOT NULL AND source_item_id <> ''
    GROUP BY observation_id"""

# Stage 3 — list/conflict aggregation ONLY for ids that actually have duplicates (~10 % of rows)
AGG_DUP_SQL = """
    SELECT n.observation_id,
      list(DISTINCT n.discovery_source)                                              AS discovery_sources,
      list(DISTINCT n.subject_query) FILTER (WHERE n.subject_query IS NOT NULL)      AS queries_seen,
      list(DISTINCT n.legacy_comp_id) FILTER (WHERE n.legacy_comp_id IS NOT NULL)    AS legacy_comp_ids,
      count(DISTINCT round(n.sold_price_usd, 2)) FILTER (WHERE n.sold_price_usd IS NOT NULL) > 1 AS price_conflict,
      count(DISTINCT n.sold_date) FILTER (WHERE n.sold_date IS NOT NULL) > 1                    AS date_conflict,
      count(DISTINCT n.image_url)  FILTER (WHERE n.image_url IS NOT NULL) > 1                   AS image_conflict
    FROM {normalized} n
    WHERE n.observation_id IN (SELECT observation_id FROM {idx} WHERE n_observations > 1)
    GROUP BY n.observation_id"""

# Stage 4 — assemble canonical: winning row + counts + (dup lists or trivial single-row lists)
CANONICAL_JOIN_SQL = """
    SELECT r.observation_id, r.source, r.source_item_id, r.title, r.sold_price_usd, 'USD' AS currency,
           r.best_offer, r.sold_date, r.captured_at, r.shipping_text, r.bids_text, r.condition,
           r.grade_company, r.grade,
           CASE WHEN r.grade_company IS NOT NULL THEN 'graded' ELSE 'raw_or_unknown' END AS raw_or_graded,
           r.source_url, r.image_url, r.subject_query AS primary_subject_query,
           i.n_observations, i.first_seen_at, i.last_seen_at,
           COALESCE(d.discovery_sources, [r.discovery_source])                                           AS discovery_sources,
           COALESCE(d.queries_seen, CASE WHEN r.subject_query IS NULL THEN [] ELSE [r.subject_query] END)  AS queries_seen,
           COALESCE(d.legacy_comp_ids, CASE WHEN r.legacy_comp_id IS NULL THEN [] ELSE [r.legacy_comp_id] END) AS legacy_comp_ids,
           COALESCE(d.price_conflict, FALSE) AS price_conflict,
           COALESCE(d.date_conflict,  FALSE) AS date_conflict,
           COALESCE(d.image_conflict, FALSE) AS image_conflict,
           r.src_file AS chosen_src_file, r.src_row AS chosen_src_row,
           '{normalizer_version}' AS normalizer_version,
           COALESCE(year(r.sold_date), 0)  AS year,
           COALESCE(month(r.sold_date), 0) AS month
    FROM {normalized} r
    JOIN {idx} i ON i.pick.src_file = r.src_file AND i.pick.src_row = r.src_row
    LEFT JOIN {agg_dup} d ON d.observation_id = r.observation_id"""

NORMALIZER_VERSION = "1.0.0"


def run_canonical_stages(con, normalized_rel: str = "normalized_t", log=None) -> str:
    """Materialize idx → agg_dup → canonical_t from a normalized relation (table name or a
    read_parquet(...) expression). Returns 'canonical_t'. Stages are narrow or duplicate-only so the
    whole thing fits a ~1 GB DuckDB cap with disk spill (one build side of ~3.25M narrow rows + 333K dup rows)."""
    log = log or (lambda *_: None)
    con.execute(f"CREATE OR REPLACE TABLE idx_t AS {IDX_SQL.format(normalized=normalized_rel)}")
    log("idx_t")
    con.execute(f"CREATE OR REPLACE TABLE agg_dup_t AS {AGG_DUP_SQL.format(normalized=normalized_rel, idx='idx_t')}")
    log("agg_dup_t")
    con.execute("CREATE OR REPLACE TABLE canonical_t AS " + CANONICAL_JOIN_SQL.format(
        normalized=normalized_rel, idx="idx_t", agg_dup="agg_dup_t", normalizer_version=NORMALIZER_VERSION))
    log("canonical_t")
    con.execute("DROP TABLE idx_t; DROP TABLE agg_dup_t")
    return "canonical_t"


def rss_mb() -> float:
    if psutil:
        return psutil.Process().memory_info().rss / 1e6
    return -1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=DEFAULT_STORE)
    ap.add_argument("--out", default=None, help="default <store>/external_store")
    ap.add_argument("--memory-limit", default="1200MB")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--files", default=",".join(STORE_FILES))
    ap.add_argument("--extra-json", default="", help="comma list of extra JSON arrays (e.g. nightly run files)")
    ap.add_argument("--skip-raw-if-exists", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="rows per file (smoke test)")
    a = ap.parse_args()

    out = a.out or os.path.join(a.store, "external_store")
    raw_dir = os.path.join(out, "raw_parquet", "ebay")  # NOT "source=ebay": Hive auto-detect would shadow the real source column
    canon_dir = os.path.join(out, "parquet", "source=ebay")
    tmp_dir = os.path.join(out, "duckdb_tmp")
    for d in (raw_dir, canon_dir, tmp_dir, os.path.join(out, "reports")):
        os.makedirs(d, exist_ok=True)
    dbpath = os.path.join(out, "canonical_v1.duckdb")
    report = {"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "store": a.store, "out": out,
              "memory_limit": a.memory_limit, "threads": a.threads, "steps": [], "host": os.uname().nodename}

    con = duckdb.connect(dbpath)
    con.execute(f"SET memory_limit='{a.memory_limit}'")
    con.execute(f"SET threads={a.threads}")
    con.execute(f"SET temp_directory='{tmp_dir}'")
    con.execute("SET preserve_insertion_order=false")

    files = [f for f in a.files.split(",") if f]
    extra = [f for f in a.extra_json.split(",") if f]
    cols = ", ".join(f"'{k}': '{v}'" for k, v in RAW_COLUMNS.items())

    # 1) raw immutable Parquet per source file (streaming read_json, bounded memory)
    raw_paths = []
    for fn in files + extra:
        src = fn if os.path.isabs(fn) else os.path.join(a.store, fn)
        base = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(raw_dir, f"{base}.parquet")
        raw_paths.append(dst)
        if a.skip_raw_if_exists and os.path.exists(dst):
            report["steps"].append({"step": "raw_parquet", "file": fn, "skipped": True})
            print(f"[raw] {fn}: exists, skipped", flush=True)
            continue
        t0 = time.time(); r0 = rss_mb()
        lim = f" LIMIT {a.limit}" if a.limit else ""
        con.execute(f"""
            COPY (
              SELECT *, '{os.path.basename(src)}' AS src_file, row_number() OVER () AS src_row
              FROM read_json('{src}', format='array', records=true, columns={{{cols}}},
                             maximum_object_size=33554432){lim}
            ) TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 200000)
        """)
        n = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
        step = {"step": "raw_parquet", "file": fn, "rows": n, "seconds": round(time.time() - t0, 1),
                "json_bytes": os.path.getsize(src), "parquet_bytes": os.path.getsize(dst), "rss_mb_after": round(rss_mb())}
        report["steps"].append(step)
        print(f"[raw] {fn}: {n:,} rows → {dst} ({step['parquet_bytes']/1e6:.0f} MB) in {step['seconds']}s, rss {step['rss_mb_after']} MB", flush=True)

    # 2) canonical dedup → materialized, spill-friendly stages (each stage can spill to temp_directory)
    t0 = time.time()
    src_expr = "read_parquet([" + ", ".join(f"'{p}'" for p in raw_paths) + "], union_by_name=true, hive_partitioning=false)"
    normalized = NORMALIZE_SQL.format(src=src_expr)
    con.execute("DROP VIEW IF EXISTS raw_rows_v; DROP VIEW IF EXISTS normalized_v; DROP VIEW IF EXISTS canonical_v")
    con.execute(f"CREATE VIEW raw_rows_v AS SELECT * FROM {src_expr}")
    def log_stage(name):
        print(f"[canon] {name}: {con.execute(f'SELECT count(*) FROM {name}').fetchone()[0]:,} rows "
              f"({time.time()-t0:.0f}s, rss {rss_mb():.0f} MB)", flush=True)
    # normalized rows → Parquet (streams on every later read; avoids holding 3.6M wide rows in the buffer pool)
    norm_path = os.path.join(out, "raw_parquet", "normalized_ebay.parquet")  # flat path, no key=value dirs
    con.execute(f"COPY ({normalized}) TO '{norm_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 200000)")
    norm_rel = f"read_parquet('{norm_path}', hive_partitioning=false)"
    con.execute("DROP TABLE IF EXISTS normalized_t")
    con.execute(f"CREATE VIEW normalized_v AS SELECT * FROM {norm_rel}")
    print(f"[canon] normalized parquet: {con.execute(f'SELECT count(*) FROM {norm_rel}').fetchone()[0]:,} rows "
          f"({time.time()-t0:.0f}s, rss {rss_mb():.0f} MB)", flush=True)
    run_canonical_stages(con, norm_rel, log=log_stage)
    # overwrite partition dir for idempotent re-runs of the prototype
    con.execute(f"""
        COPY canonical_t TO '{canon_dir}'
        (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (year, month), OVERWRITE_OR_IGNORE true, ROW_GROUP_SIZE 200000)
    """)
    con.execute(f"CREATE VIEW canonical_v AS SELECT * FROM read_parquet('{canon_dir}/**/*.parquet', hive_partitioning=true)")
    canon_seconds = round(time.time() - t0, 1)
    print(f"[canon] partitioned parquet written ({canon_seconds}s total)", flush=True)

    # 3) audit numbers
    t0 = time.time()
    q = lambda sql: con.execute(sql).fetchone()
    total_rows = q("SELECT count(*) FROM raw_rows_v")[0]
    canon_rows = q("SELECT count(*) FROM canonical_v")[0]
    no_id = q("SELECT count(*) FROM normalized_v WHERE source_item_id IS NULL OR source_item_id=''")[0]
    stats = con.execute("""
        SELECT
          sum(CASE WHEN n_observations>1 THEN 1 ELSE 0 END)       AS ids_with_dupes,
          sum(n_observations-1)                                    AS duplicates_removed,
          sum(CASE WHEN price_conflict THEN 1 ELSE 0 END)          AS price_conflicts,
          sum(CASE WHEN date_conflict  THEN 1 ELSE 0 END)          AS date_conflicts,
          sum(CASE WHEN image_conflict THEN 1 ELSE 0 END)          AS image_conflicts,
          sum(CASE WHEN sold_price_usd IS NULL THEN 1 ELSE 0 END)  AS price_missing,
          sum(CASE WHEN sold_date IS NULL THEN 1 ELSE 0 END)       AS sold_date_missing,
          sum(CASE WHEN best_offer IS NULL THEN 1 ELSE 0 END)      AS best_offer_unknown,
          sum(CASE WHEN best_offer THEN 1 ELSE 0 END)              AS best_offer_true,
          sum(CASE WHEN image_url IS NULL OR image_url='' THEN 1 ELSE 0 END) AS image_missing,
          sum(CASE WHEN grade_company IS NOT NULL THEN 1 ELSE 0 END) AS graded_rows,
          sum(CASE WHEN len(discovery_sources)>1 THEN 1 ELSE 0 END) AS multi_source_ids,
          sum(CASE WHEN len(queries_seen)>1 THEN 1 ELSE 0 END)      AS multi_query_ids,
          min(sold_date), max(sold_date), min(first_seen_at), max(last_seen_at)
        FROM canonical_v
    """).fetchone()
    by_month = con.execute("""SELECT year, month, count(*) FROM canonical_v WHERE year>=2025 GROUP BY 1,2 ORDER BY 1,2""").fetchall()
    by_source = con.execute("""SELECT s, count(*) FROM (SELECT unnest(discovery_sources) AS s FROM canonical_v) GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    # query latency sample (PRD: "do not query giant raw JSON at runtime")
    tq = time.time()
    con.execute("""SELECT count(*), median(sold_price_usd) FROM canonical_v
                   WHERE best_offer = FALSE AND lower(title) LIKE '%wembanyama%' AND grade_company='PSA' AND grade LIKE 'PSA 10%'""").fetchone()
    latency_ms = round((time.time() - tq) * 1000)
    audit_seconds = round(time.time() - t0, 1)

    parquet_bytes = 0
    for root, _, fs in os.walk(canon_dir):
        parquet_bytes += sum(os.path.getsize(os.path.join(root, f)) for f in fs if f.endswith(".parquet"))
    raw_bytes = sum(os.path.getsize(p) for p in raw_paths if os.path.exists(p))

    report.update({
        "raw_rows_total": total_rows,
        "canonical_rows": canon_rows,
        "rows_without_item_id": no_id,
        "ids_with_dupes": stats[0], "duplicates_removed": stats[1],
        "price_conflicts": stats[2], "date_conflicts": stats[3], "image_conflicts": stats[4],
        "price_missing": stats[5], "sold_date_missing": stats[6], "best_offer_unknown": stats[7],
        "best_offer_true": stats[8], "image_missing": stats[9], "graded_rows": stats[10],
        "multi_source_ids": stats[11], "multi_query_ids": stats[12],
        "sold_date_min": str(stats[13]), "sold_date_max": str(stats[14]),
        "first_seen_min": str(stats[15]), "last_seen_max": str(stats[16]),
        "canonical_by_month_2025plus": [{"year": y, "month": m, "rows": c} for y, m, c in by_month],
        "discovery_source_counts": [{"source": s, "ids": c} for s, c in by_source],
        "canonical_seconds": canon_seconds, "audit_seconds": audit_seconds,
        "sample_query_latency_ms": latency_ms,
        "raw_parquet_bytes": raw_bytes, "canonical_parquet_bytes": parquet_bytes,
        "duckdb_file": dbpath, "peak_rss_mb": round(rss_mb()),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "originals_modified": False,
    })
    rp = os.path.join(out, "reports", f"migration_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(rp, "w") as f:
        json.dump(report, f, indent=1, default=str)
    latest = os.path.join(out, "reports", "migration_report_latest.json")
    try:
        if os.path.lexists(latest):
            os.remove(latest)
        os.symlink(os.path.basename(rp), latest)
    except OSError:
        pass
    con.close()
    print(json.dumps({k: v for k, v in report.items() if k not in ("steps", "canonical_by_month_2025plus", "discovery_source_counts")}, indent=1, default=str))
    print(f"[report] {rp}")


if __name__ == "__main__":
    main()
