# Canonical store crosses 10.9M sales — SCP canonicalized (2026-08-21)

## Headline

| | before tonight | after |
|---|---|---|
| canonical rows | 4,415,598 | **10,903,631** |
| sources | 7 | 8 |
| latest sale | 2026-08-19 | **2026-08-21 (today)** |

`sportscardspro` — the broad catalog scrub — is now canonicalized: **6,488,033 sales, 2020-04-01 → today**,
100 % priced, 100 % carrying the underlying eBay item id.

## The finding that changes our source strategy

Earlier analysis (`SCP_VS_EBAY_COVERAGE_20260819.md`) said SCP "substitutes for eBay on sports" because SCP's
prices are eBay-derived. **Measured against the actual data, that framing was wrong in an important way.**

```
SCP distinct eBay item ids : 6,487,395
eBay distinct item ids     : 4,261,251
OVERLAP (same sale in both):       243   ← 0.0 %
SCP sales eBay NEVER had   : 6,487,152   ← net-new
```

**The two corpora are almost entirely disjoint.** Not redundant — complementary.

**Why:** our eBay corpus was built from *player-name and bulk-category searches* (a demand-driven slice of
eBay's sold data). SCP walks the *entire card catalog* card-by-card (a supply-driven slice). Both sample eBay's
sold listings; they land on almost completely different sales. eBay's sold universe is far larger than either
approach captures alone.

**Consequences:**
1. SCP is not a stopgap for the eBay block — it is a **primary source in its own right**, and the single
   largest one we have.
2. The `ledger_anchor` → `ebay_item_id` extraction gives SCP rows **item-level identity**, which corrects the
   earlier claim that only eBay could supply it. Cross-source dedup is now possible and measured (currently a
   non-issue at 243 rows).
3. Double-counting risk between SCP and eBay is **negligible today** — no merge policy needed yet. Re-measure
   after any eBay resumption, since a future eBay sweep could overlap SCP's catalog coverage.

## The post-block window is covered

eBay data stops dead at **2026-06-25** (the block). SCP covers straight through:

| month | SCP sales |
|---|---|
| 2026-07 | 394,330 |
| 2026-08 | 203,774 (partial, through the 21st) |

Combined with TCGplayer (63,524 rows of Pokémon/TCG, 2026-07-07 → 2026-08-19), **the "8-week hole" is closed**
for both sports and TCG — without eBay.

## How it's stored

`parquet/source=sportscardspro/year=/month=/` — its own partition, canonical column contract, originals in
`~/mazi_scp_broad/scp_broad_comps.jsonl` untouched. Dedup within the snapshot on `(observation_id, sold_date)`
collapsed 8,067,024 raw rows → 6,488,033 canonical (1.58 M within-file repeats from overlapping catalog sweeps).
Ingest: 793 s under a 1.2 GB memory cap, single thread, alongside the live capture fleet.

Extra columns SCP contributes: `ebay_item_id` (cross-source key), `grade`/`grade_company` (from `grade_label`),
`best_offer`.

## Refresh

The SCP broad scrub runs continuously (supervisor healer keeps it alive). Re-canonicalize after it accumulates:

```
.venv_extcomps/bin/python external_engine/multi_source_ingest.py --only scp_broad --memory-limit 1200MB
.venv_extcomps/bin/python external_engine/classifier_v1.py --memory-limit 1200MB
```

*Candidate improvement: make this a governed supervisor lane so canonical tracks the scrub automatically
rather than on demand.*
