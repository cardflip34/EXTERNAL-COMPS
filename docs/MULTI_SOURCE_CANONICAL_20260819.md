# Multi-source canonical store — 2026-08-19

`external_market_canonical_v1` now spans **7 sources** (was eBay-only). Non-eBay sources are normalized from
their live JSON snapshots by `external_engine/multi_source_ingest.py` into `parquet/source=<src>/year=/month=/`
using the same canonical column contract; the canonical read is `parquet/**/*.parquet` (hive-partitioned).

| source | rows | sold_date span | notes |
|---|---|---|---|
| ebay | 4,264,776 | 2015-04 → 2026-06-25 | append+index writer; item-level (item_id, image, OBO) |
| tcgplayer | 63,524 | **2026-07-07 → 2026-08-19** | **TCG/Pokémon, NOT eBay-derived — fills the non-sports post-block gap SCP can't** |
| fanatics | 44,025 | 2009-10 → 2026-06 | auction house; buyer premium captured |
| goldin | 41,457 | 2025-01 → 2026-07 | auction house; `premium_pct` on all rows |
| rea | 914 | 2004-05 → 2026-07 | vintage sports realized |
| auctionreport | 455 | 2026-06 → 2026-08 | multi-house top lots (RSS) |
| myslabs | 447 | 2026-07 → 2026-08 | graded marketplace |
| **TOTAL** | **4,415,598** | | |

## Design
- **Snapshot semantics**: each non-eBay file is a full re-scrape, so ingest OVERWRITES that source's partition
  (idempotent; dedup within file by `(observation_id, sold_date)`). eBay stays on the append+index writer.
- **IDs**: `observation_id = '<PREFIX>:<comp_id | sha1(url)>'` (GOLDIN:, FANATICS:, TCG:, MYSLABS:, REA:, AR:).
- **Prices**: realized/net → `best_offer=FALSE`, `valuation_gate='ok'` unless lot/no-price. Buyer premium kept in
  `premium_pct`/`premium_abs` where the source exposes it (auction houses) for future all-in computation.
- **Classification**: `classifier_v1` runs over all sources (4.42M rows); category pokemon rose to 511K with TCGplayer.
- **Non-destructive**: original JSON files untouched; each source's partition is reproducible from its snapshot.

## Refresh
Re-run after the sources lane updates a snapshot:
`.venv_extcomps/bin/python external_engine/multi_source_ingest.py [--only <src>]` then `classifier_v1.py`.
(Candidate: add this as a governed healer/lane step so canonical tracks the live snapshots automatically.)

## Not included (see EXTERNAL_SOURCE_ACCESS_MATRIX.md)
Heritage/Pristine (stale selectors), COMC (list not sold), StarStock (dead site) — not deployable as-is.
