#!/usr/bin/env python3
"""Multi-source canonicalization (PRD V2 §6/§25) — normalize non-eBay comp stores into the SAME
external_market_canonical_v1, as source-partitioned Parquet, NON-DESTRUCTIVELY.

The live non-eBay sources (Goldin, Fanatics, TCGplayer, MySlabs, REA, AuctionReport) land as JSON snapshots
in ~/whatnot-sniper. This ingests each into parquet/source=<src>/year=/month=/ using the canonical column
contract, so `canonical_v` (read across parquet/**) spans every source with one schema.

Snapshot model: each file is a FULL re-scrape, so per-source ingest OVERWRITES that source's partition
(idempotent by construction; dedup within the file by observation_key). eBay stays on the append+index writer.
Auction realized prices are treated as verified sales (valuation_gate='ok' unless lot/no-price); buyer premium
is preserved where the source exposes it. observation_id = '<PREFIX>:<comp_id or sha1(url)>'.

Usage (lane venv):
  .venv_extcomps/bin/python external_engine/multi_source_ingest.py [--only goldin,fanatics] [--store DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import canonical_migrate as cm  # noqa: E402  (VALUATION_GATE_SQL, NORMALIZER_VERSION)

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
PROJECT = ROOT
NORMALIZER_VERSION = "ms-1.0.0"

# Per-source field map. price/date/url/title/image/id are SQL expressions over the raw row columns.
# All sources: sold prices are realized/net (NOT asking), so best_offer=FALSE. `premium_pct`/`premium_abs`
# capture buyer premium where present (auction houses). date_fmt handles the two eBay-style spellings + ISO.
SPECS = {
    "goldin": {
        "file": "goldin_comps_v2.json", "prefix": "GOLDIN", "label": "goldin",
        "cols": {"comp_id": "VARCHAR", "title": "VARCHAR", "sold_price": "VARCHAR", "sold_date": "VARCHAR",
                 "url": "VARCHAR", "lot_id": "VARCHAR", "buyer_premium_pct": "VARCHAR", "primary_image_name": "VARCHAR",
                 "auction_title": "VARCHAR", "scraped_at": "VARCHAR"},
        "id": "COALESCE(NULLIF(comp_id,''), NULLIF(lot_id,''), sha1(url))",
        "image": "NULL", "premium_pct": "TRY_CAST(buyer_premium_pct AS DOUBLE)", "shipping": "NULL",
        "subject": "auction_title",
    },
    "fanatics": {
        "file": "fanatics_comps.json", "prefix": "FANATICS", "label": "fanatics",
        "cols": {"comp_id": "VARCHAR", "title": "VARCHAR", "sold_price": "VARCHAR", "sold_date": "VARCHAR",
                 "url": "VARCHAR", "image_url": "VARCHAR", "venue": "VARCHAR", "buyers_premium": "VARCHAR", "scraped_at": "VARCHAR"},
        "id": "COALESCE(NULLIF(comp_id,''), sha1(url))",
        "image": "image_url", "premium_pct": "NULL", "premium_abs": "TRY_CAST(regexp_replace(buyers_premium,'[$,]','','g') AS DOUBLE)",
        "shipping": "NULL", "subject": "venue",
    },
    "tcgplayer": {
        "file": "tcgplayer_sold_comps.json", "prefix": "TCG", "label": "tcgplayer",
        "cols": {"comp_id": "VARCHAR", "title": "VARCHAR", "sold_price": "VARCHAR", "shipping": "VARCHAR",
                 "sold_date": "VARCHAR", "url": "VARCHAR", "set_name": "VARCHAR", "number": "VARCHAR",
                 "rarity": "VARCHAR", "product_id": "VARCHAR", "variant": "VARCHAR", "condition": "VARCHAR"},
        "id": "COALESCE(NULLIF(comp_id,''), NULLIF(product_id,'') || '-' || NULLIF(sold_date,''), sha1(url))",
        "image": "NULL", "premium_pct": "NULL", "shipping": "shipping", "subject": "set_name", "condition": "condition",
    },
    "myslabs": {
        "file": "myslabs_comps.json", "prefix": "MYSLABS", "label": "myslabs",
        "cols": {"comp_id": "VARCHAR", "title": "VARCHAR", "sold_price": "VARCHAR", "shipping": "VARCHAR",
                 "sold_date": "VARCHAR", "url": "VARCHAR", "image_url": "VARCHAR", "scraped_at": "VARCHAR"},
        "id": "COALESCE(NULLIF(comp_id,''), sha1(url))",
        "image": "image_url", "premium_pct": "NULL", "shipping": "shipping", "subject": "NULL",
    },
    "rea": {
        "file": "rea_comps.json", "prefix": "REA", "label": "rea",
        "cols": {"comp_id": "VARCHAR", "title": "VARCHAR", "sold_price": "VARCHAR", "sold_date": "VARCHAR",
                 "url": "VARCHAR", "image_url": "VARCHAR", "year": "VARCHAR", "lot": "VARCHAR"},
        "id": "COALESCE(NULLIF(comp_id,''), NULLIF(lot,''), sha1(url))",
        "image": "image_url", "premium_pct": "NULL", "shipping": "NULL", "subject": "NULL",
    },
    "auctionreport": {
        "file": "auctionreport_comps.json", "prefix": "AR", "label": "auctionreport",
        "cols": {"comp_id": "VARCHAR", "title": "VARCHAR", "sold_price": "VARCHAR", "sold_date": "VARCHAR",
                 "auction_house": "VARCHAR", "post_url": "VARCHAR", "post_title": "VARCHAR", "scraped_at": "VARCHAR"},
        "id": "COALESCE(NULLIF(comp_id,''), sha1(COALESCE(post_url,'') || title))",
        "url": "post_url", "image": "NULL", "premium_pct": "NULL", "shipping": "NULL", "subject": "auction_house",
    },
}

# SCP broad is special: JSONL (not an array), 8M+ rows, and a nested `raw` struct carrying the underlying
# eBay item id (`ledger_anchor` = 'ebay-<itemid>'), the grade label, and the best-offer flag. Handled by its
# own function rather than the generic SPEC path.
SCP_JSONL = os.path.expanduser("~/mazi_scp_broad/scp_broad_comps.jsonl")
SCP_COLUMNS = ("{'catalog_id':'VARCHAR','source_item_id':'VARCHAR','title':'VARCHAR','sold_price':'VARCHAR',"
               "'sold_date':'VARCHAR','source_url':'VARCHAR','image_url':'VARCHAR','slug':'VARCHAR',"
               "'raw':'STRUCT(source VARCHAR, slug VARCHAR, ledger_anchor VARCHAR, grade_label VARCHAR, "
               "image_url VARCHAR, scp_original_title VARCHAR, \"soldDate\" VARCHAR, best_offer VARCHAR, broad_scrub VARCHAR)'}")


def ingest_scp_broad(con, out: str, dry_run: bool, jsonl: str = SCP_JSONL, limit: int | None = None) -> dict:
    """Canonicalize the SCP broad scrub JSONL (sports historical archive) into source=sportscardspro.

    Keeps SCP as its own source/partition (provenance preserved, originals untouched) but extracts
    `ebay_item_id` from raw.ledger_anchor so cross-source overlap with the eBay partition is MEASURABLE —
    the merge policy is then an evidence-based decision, not a guess."""
    if not os.path.exists(jsonl):
        return {"source": "scp_broad", "skipped": "file_absent", "path": jsonl}
    t0 = time.time()
    lim = f" LIMIT {limit}" if limit else ""
    price = "TRY_CAST(regexp_extract(regexp_replace(COALESCE(sold_price,''),'[$,\\s]','','g'),'-?[0-9]+(\\.[0-9]+)?',0) AS DOUBLE)"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE scp_norm AS
        SELECT 'SCP:' || source_item_id                                        AS observation_id,
               source_item_id,
               title,
               {price}                                                          AS sold_price_usd,
               TRY_CAST(substr(COALESCE(sold_date,''),1,10) AS DATE)            AS sold_date,
               source_url, image_url, slug,
               NULLIF(regexp_extract(COALESCE(raw.ledger_anchor,''),'ebay-([0-9]+)',1),'') AS ebay_item_id,
               NULLIF(raw.grade_label,'')                                       AS grade_label,
               CASE lower(COALESCE(raw.best_offer,'')) WHEN 'true' THEN TRUE WHEN 'false' THEN FALSE END AS best_offer,
               row_number() OVER ()                                             AS src_row
        FROM read_json('{jsonl}', format='newline_delimited', records=true, columns={SCP_COLUMNS},
                       maximum_object_size=33554432, ignore_errors=true){lim}""")
    seen = con.execute("SELECT count(*) FROM scp_norm").fetchone()[0]
    con.execute("""CREATE OR REPLACE TEMP TABLE scp_dedup AS
        SELECT * EXCLUDE (rn) FROM (
          SELECT *, row_number() OVER (PARTITION BY observation_id, sold_date
                    ORDER BY (sold_price_usd IS NULL), src_row) rn FROM scp_norm
          WHERE source_item_id IS NOT NULL) WHERE rn=1""")
    rows = con.execute("SELECT count(*) FROM scp_dedup").fetchone()[0]
    gate = cm.VALUATION_GATE_SQL.replace("{p}.best_offer", "COALESCE(best_offer,FALSE)").replace("{p}.", "")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE scp_canon AS
        SELECT observation_id || ':' || COALESCE(CAST(sold_date AS VARCHAR),'nodate') AS observation_key,
               observation_id, 'sportscardspro' AS source, source_item_id, title, sold_price_usd,
               'USD' AS currency, COALESCE(best_offer,FALSE) AS best_offer, sold_date,
               NULL::TIMESTAMP AS captured_at, NULL AS shipping_text, NULL AS bids_text, NULL AS condition,
               CASE WHEN grade_label IS NULL THEN NULL
                    WHEN regexp_matches(lower(title),'\\\\bpsa\\\\b') THEN 'PSA'
                    WHEN regexp_matches(lower(title),'\\\\bbgs\\\\b') THEN 'BGS'
                    WHEN regexp_matches(lower(title),'\\\\bsgc\\\\b') THEN 'SGC'
                    WHEN regexp_matches(lower(title),'\\\\bcgc\\\\b') THEN 'CGC' ELSE 'UNKNOWN' END AS grade_company,
               grade_label AS grade,
               CASE WHEN grade_label IS NOT NULL THEN 'graded' ELSE 'raw_or_unknown' END AS raw_or_graded,
               source_url, image_url, slug AS primary_subject_query,
               1::BIGINT AS n_observations, NULL::TIMESTAMP AS first_seen_at, NULL::TIMESTAMP AS last_seen_at,
               ['sportscardspro'] AS discovery_sources,
               CASE WHEN slug IS NULL THEN [] ELSE [slug] END AS queries_seen,
               [] AS legacy_comp_ids,
               FALSE AS price_conflict, FALSE AS date_conflict, FALSE AS image_conflict,
               ({gate}) AS valuation_gate,
               NULL::DOUBLE AS premium_pct, NULL::DOUBLE AS premium_abs,
               ebay_item_id,
               'scp_broad_comps.jsonl' AS chosen_src_file, src_row AS chosen_src_row,
               '{NORMALIZER_VERSION}' AS normalizer_version,
               COALESCE(year(sold_date),0) AS year, COALESCE(month(sold_date),0) AS month
        FROM scp_dedup""")
    priced = con.execute("SELECT count(*) FROM scp_canon WHERE sold_price_usd IS NOT NULL").fetchone()[0]
    with_ebay_id = con.execute("SELECT count(*) FROM scp_canon WHERE ebay_item_id IS NOT NULL").fetchone()[0]
    span = con.execute("SELECT min(sold_date), max(sold_date) FROM scp_canon").fetchone()
    part = os.path.join(out, "parquet", "source=sportscardspro")
    if not dry_run:
        import shutil
        if os.path.isdir(part):
            shutil.rmtree(part)
        os.makedirs(part, exist_ok=True)
        con.execute(f"""COPY scp_canon TO '{part}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (year, month),
                        OVERWRITE_OR_IGNORE true, ROW_GROUP_SIZE 100000)""")
    return {"source": "scp_broad", "rows_seen": seen, "rows_canonical": rows, "priced": priced,
            "with_ebay_item_id": with_ebay_id, "sold_date_min": str(span[0]), "sold_date_max": str(span[1]),
            "partition": part, "seconds": round(time.time()-t0, 1), "dry_run": dry_run}



# Fanatics moved to Neon-export staging 2026-09-06: the live v3 pipeline writes Neon only, so the legacy
# fanatics_comps.json froze at Jun 22 and canonical lagged 76 days (caught by freshness_report's first run).
# Ingest = staging JSONL (Neon superset, incremental via neon_source_export.py) ∪ legacy JSON (tiny; keeps any
# row that never bridged), dedup by (observation_id, sold_date), OVERWRITE the fanatics partition.
FAN_STAGING = os.path.join(STORE, "external_store", "staging", "fanatics_neon.jsonl")
FAN_COLS = ("{'comp_id':'VARCHAR','title':'VARCHAR','sold_price':'VARCHAR','sold_date':'VARCHAR','url':'VARCHAR',"
            "'image_url':'VARCHAR','grade':'VARCHAR','grader':'VARCHAR','best_offer':'VARCHAR',"
            "'buyers_premium':'VARCHAR','scraped_at':'VARCHAR'}")


def ingest_fanatics_neon(con, out: str, dry_run: bool) -> dict:
    if not os.path.exists(FAN_STAGING):
        return {"source": "fanatics_neon", "skipped": "staging_absent (run neon_source_export.py)", "path": FAN_STAGING}
    t0 = time.time()
    legacy = os.path.join(PROJECT, "fanatics_comps.json")
    spec_legacy = SPECS["fanatics"]
    lcols = ", ".join(f"'{k}': '{v}'" for k, v in spec_legacy["cols"].items())
    price = "TRY_CAST(regexp_extract(regexp_replace(COALESCE(sold_price,''),'[$,\\s]','','g'),'-?[0-9]+(\\.[0-9]+)?',0) AS DOUBLE)"
    date_l = DATE_SQL.format(d="sold_date")
    parts = [f"""
        SELECT COALESCE(NULLIF(comp_id,''), sha1(url)) AS rid, title, {price} AS sold_price_usd, {date_l} AS sold_date,
               url AS source_url, image_url, NULLIF(grade,'') AS grade, NULLIF(grader,'') AS grade_company,
               CASE lower(COALESCE(best_offer,'')) WHEN 'true' THEN TRUE ELSE FALSE END AS best_offer,
               TRY_CAST(scraped_at AS TIMESTAMP) AS captured_at, 'neon' AS src_file, row_number() OVER () AS src_row
        FROM read_json('{FAN_STAGING}', format='newline_delimited', records=true, columns={FAN_COLS},
                       maximum_object_size=33554432, ignore_errors=true)"""]
    if os.path.exists(legacy):
        parts.append(f"""
        SELECT COALESCE(NULLIF(comp_id,''), sha1(url)) AS rid, title, {price} AS sold_price_usd, {date_l} AS sold_date,
               url AS source_url, image_url, NULL AS grade, NULL AS grade_company, FALSE AS best_offer,
               TRY_CAST(scraped_at AS TIMESTAMP) AS captured_at, 'legacy_json' AS src_file,
               1000000000 + row_number() OVER () AS src_row
        FROM read_json('{legacy}', format='array', records=true, columns={{{lcols}}}, maximum_object_size=33554432)""")
    con.execute("CREATE OR REPLACE TEMP TABLE fan_norm AS " + " UNION ALL ".join(parts))
    seen = con.execute("SELECT count(*) FROM fan_norm").fetchone()[0]
    con.execute("""CREATE OR REPLACE TEMP TABLE fan_dedup AS
        SELECT * EXCLUDE (rn) FROM (
          SELECT *, row_number() OVER (PARTITION BY rid, sold_date
                    ORDER BY (grade IS NULL), (sold_price_usd IS NULL), src_row) rn
          FROM fan_norm WHERE rid IS NOT NULL) WHERE rn=1""")
    rows = con.execute("SELECT count(*) FROM fan_dedup").fetchone()[0]
    gate = cm.VALUATION_GATE_SQL.replace("{p}.best_offer", "best_offer").replace("{p}.", "")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE fan_canon AS
        SELECT 'FANATICS:' || rid || ':' || COALESCE(CAST(sold_date AS VARCHAR),'nodate') AS observation_key,
               'FANATICS:' || rid AS observation_id, 'fanatics' AS source, rid AS source_item_id, title,
               sold_price_usd, 'USD' AS currency, best_offer, sold_date, captured_at,
               NULL AS shipping_text, NULL AS bids_text, NULL AS condition,
               grade_company, grade,
               CASE WHEN grade IS NOT NULL THEN 'graded' ELSE 'raw_or_unknown' END AS raw_or_graded,
               source_url, image_url, NULL AS primary_subject_query,
               1::BIGINT AS n_observations, captured_at AS first_seen_at, captured_at AS last_seen_at,
               ['fanatics'] AS discovery_sources, [] AS queries_seen, [] AS legacy_comp_ids,
               FALSE AS price_conflict, FALSE AS date_conflict, FALSE AS image_conflict,
               ({gate}) AS valuation_gate, NULL::DOUBLE AS premium_pct, NULL::DOUBLE AS premium_abs,
               src_file AS chosen_src_file, src_row AS chosen_src_row,
               '{NORMALIZER_VERSION}' AS normalizer_version,
               COALESCE(year(sold_date),0) AS year, COALESCE(month(sold_date),0) AS month
        FROM fan_dedup""")
    priced = con.execute("SELECT count(*) FROM fan_canon WHERE sold_price_usd IS NOT NULL").fetchone()[0]
    span = con.execute("SELECT min(sold_date), max(sold_date) FROM fan_canon").fetchone()
    part = os.path.join(out, "parquet", "source=fanatics")
    if not dry_run:
        import shutil
        if os.path.isdir(part):
            shutil.rmtree(part)
        os.makedirs(part, exist_ok=True)
        con.execute(f"""COPY fan_canon TO '{part}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (year, month),
                        OVERWRITE_OR_IGNORE true, ROW_GROUP_SIZE 100000)""")
    return {"source": "fanatics_neon", "rows_seen": seen, "rows_canonical": rows, "priced": priced,
            "sold_date_min": str(span[0]), "sold_date_max": str(span[1]), "partition": part,
            "seconds": round(time.time()-t0, 1), "dry_run": dry_run}


DATE_SQL = """COALESCE(
    TRY_CAST(TRY_STRPTIME(regexp_replace(COALESCE({d},''), '^(Sold|Ended)\\s+', ''), '%b %d, %Y') AS DATE),
    TRY_CAST(TRY_STRPTIME(regexp_replace(COALESCE({d},''), '^(Sold|Ended)\\s+', ''), '%B %d, %Y') AS DATE),
    TRY_CAST(substr(COALESCE({d},''), 1, 10) AS DATE))"""


def ingest_source(con, key: str, spec: dict, out: str, dry_run: bool) -> dict:
    src_path = os.path.join(PROJECT, spec["file"])
    if not os.path.exists(src_path):
        return {"source": key, "skipped": "file_absent", "path": src_path}
    cols = ", ".join(f"'{k}': '{v}'" for k, v in spec["cols"].items())
    price = "TRY_CAST(regexp_extract(regexp_replace(COALESCE(sold_price,''), '[$,\\s]', '', 'g'), '-?[0-9]+(\\.[0-9]+)?', 0) AS DOUBLE)"
    date = DATE_SQL.format(d="sold_date")
    urlexpr = spec.get("url", "url")
    image = spec.get("image", "NULL")
    premium_pct = spec.get("premium_pct", "NULL")
    premium_abs = spec.get("premium_abs", "NULL")
    shipping = spec.get("shipping", "NULL")
    subject = spec.get("subject", "NULL")
    condition = spec.get("condition", "NULL")
    obs_id = f"'{spec['prefix']}:' || ({spec['id']})"
    t0 = time.time()
    con.execute("DROP TABLE IF EXISTS ms_raw")
    con.execute(f"""CREATE TEMP TABLE ms_raw AS
        SELECT *, row_number() OVER () AS src_row FROM read_json('{src_path}', format='array', records=true,
               columns={{{cols}}}, maximum_object_size=33554432)""")
    seen = con.execute("SELECT count(*) FROM ms_raw").fetchone()[0]
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ms_norm AS
        SELECT {obs_id} AS observation_id, {price} AS sold_price_usd, {date} AS sold_date,
               title, {urlexpr} AS source_url, {image} AS image_url, {subject} AS subject,
               {premium_pct} AS premium_pct, {premium_abs} AS premium_abs, {shipping} AS shipping_text,
               {condition} AS condition, NULLIF(comp_id,'') AS legacy_comp_id,
               '{spec['file']}' AS src_file, src_row
        FROM ms_raw WHERE {spec['id']} IS NOT NULL""")
    # dedup within snapshot by (observation_id, sold_date); prefer priced, earliest src_row
    con.execute("""CREATE OR REPLACE TEMP TABLE ms_dedup AS
        SELECT * EXCLUDE (rn) FROM (
          SELECT *, row_number() OVER (PARTITION BY observation_id, sold_date
                    ORDER BY (sold_price_usd IS NULL), src_row) rn FROM ms_norm) WHERE rn=1""")
    rows = con.execute("SELECT count(*) FROM ms_dedup").fetchone()[0]
    # valuation gate: multi-source realized prices are never OBO, so the best_offer branch is FALSE.
    gate = cm.VALUATION_GATE_SQL.replace("{p}.best_offer", "FALSE").replace("{p}.", "")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ms_canon AS
        SELECT observation_id || ':' || COALESCE(CAST(sold_date AS VARCHAR),'nodate') AS observation_key,
               observation_id, '{spec['label']}' AS source, observation_id AS source_item_id, title, sold_price_usd,
               'USD' AS currency, FALSE AS best_offer, sold_date, NULL::TIMESTAMP AS captured_at,
               shipping_text, NULL AS bids_text, condition,
               NULL AS grade_company, NULL AS grade, 'raw_or_unknown' AS raw_or_graded,
               source_url, image_url, subject AS primary_subject_query,
               1::BIGINT AS n_observations, NULL::TIMESTAMP AS first_seen_at, NULL::TIMESTAMP AS last_seen_at,
               ['{spec['label']}'] AS discovery_sources,
               CASE WHEN subject IS NULL THEN [] ELSE [subject] END AS queries_seen,
               CASE WHEN legacy_comp_id IS NULL THEN [] ELSE [legacy_comp_id] END AS legacy_comp_ids,
               FALSE AS price_conflict, FALSE AS date_conflict, FALSE AS image_conflict,
               ({gate}) AS valuation_gate,
               premium_pct, premium_abs,
               src_file AS chosen_src_file, src_row AS chosen_src_row,
               '{NORMALIZER_VERSION}' AS normalizer_version,
               COALESCE(year(sold_date),0) AS year, COALESCE(month(sold_date),0) AS month
        FROM ms_dedup""")
    part = os.path.join(out, "parquet", f"source={spec['label']}")
    priced = con.execute("SELECT count(*) FROM ms_canon WHERE sold_price_usd IS NOT NULL").fetchone()[0]
    span = con.execute("SELECT min(sold_date), max(sold_date) FROM ms_canon").fetchone()
    if not dry_run:
        # OVERWRITE this source's partition (snapshot semantics)
        import shutil
        if os.path.isdir(part):
            shutil.rmtree(part)
        os.makedirs(part, exist_ok=True)
        con.execute(f"""COPY ms_canon TO '{part}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (year, month),
                        OVERWRITE_OR_IGNORE true, ROW_GROUP_SIZE 100000)""")
    return {"source": key, "rows_seen": seen, "rows_canonical": rows, "priced": priced,
            "sold_date_min": str(span[0]), "sold_date_max": str(span[1]), "partition": part,
            "seconds": round(time.time()-t0, 1), "dry_run": dry_run}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="comma list of sources (default all)")
    ap.add_argument("--store", default=STORE); ap.add_argument("--memory-limit", default="800MB")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="rows (scp smoke test)")
    a = ap.parse_args()
    out = os.path.join(a.store, "external_store")
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{a.memory_limit}'; SET threads=1; SET temp_directory='{os.path.join(out,'duckdb_tmp')}'")
    default_keys = [k for k in SPECS if k != "fanatics"] + ["scp_broad", "fanatics_neon"]
    keys = a.only.split(",") if a.only else default_keys
    results = [ingest_source(con, k, SPECS[k], out, a.dry_run) for k in keys if k in SPECS]
    if "scp_broad" in keys:
        results.append(ingest_scp_broad(con, out, a.dry_run, limit=a.limit))
    if "fanatics_neon" in keys:
        results.append(ingest_fanatics_neon(con, out, a.dry_run))
    con.close()
    report = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "normalizer_version": NORMALIZER_VERSION,
              "dry_run": a.dry_run, "results": results}
    rp = os.path.join(out, "reports", f"multi_source_ingest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    json.dump(report, open(rp, "w"), indent=1, default=str)
    print(json.dumps(report, indent=1, default=str)); print("[report]", rp)


if __name__ == "__main__":
    main()
