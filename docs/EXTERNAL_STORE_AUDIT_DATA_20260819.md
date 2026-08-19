# External comps store audit — 2026-08-19T18:09:50Z

Store: `/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store`  |  elapsed 135.2s  |  limit=None

## Combined

| metric | value |
|---|---|
| total_rows | 3,633,806 |
| unique_item_ids | 3,255,365 |
| duplicate_rows_total | 378,441 |
| duplicate_pct | 10.41 |
| rows_without_item_id | 0 |

### Item-id overlap between files

| files | unique item ids |
|---|---|
| raw_player_comps | 1,621,839 |
| ebay_comps | 938,417 |
| player_comps | 468,132 |
| ebay_comps+player_comps | 181,073 |
| ebay_comps+raw_player_comps | 43,103 |
| ebay_comps+player_comps+raw_player_comps | 2,801 |

## Per file

### ebay_comps.json

| metric | value | % |
|---|---|---|
| rows | 1,165,394 | 100.00 |
| unique_item_ids | 1,165,394 | 100.00 |
| intra_file_dupes | 0 | 0.00 |
| no_item_id | 0 | 0.00 |
| price_ok | 1,165,394 | 100.00 |
| price_zero | 0 | 0.00 |
| price_missing | 0 | 0.00 |
| best_offer_true | 439,570 | 37.72 |
| best_offer_missing | 0 | 0.00 |
| image_url_present | 1,165,394 | 100.00 |
| shipping_present | 405,611 | 34.80 |
| grader_present | 0 | 0.00 |
| grade_present | 0 | 0.00 |
| player_query_present | 463,347 | 39.76 |
| sold_month range | 2015-11 → 2026-04 | |
| max scraped_at | 2026-04-21T09:37:33.680952 | |

**schema variant:** v1_sold_price=702,047, v2_price=463,347

**source:** ebay=702,047, =463,347

**comp_id prefix:** EB=1,165,394

**category_v0 (keyword heuristic):** sports=998,893, coins=67,228, pokemon=58,280, yugioh=33,432, veefriends=6,874, watches=687

**sport_v0 (keyword heuristic, low confidence):** basketball=390,470, unknown=323,699, baseball=113,742, hockey=46,611, football=33,915, wrestling=31,636, soccer=22,705, wnba=14,245, golf=7,219, ufc_mma=4,797, racing=4,526, boxing=3,609, tennis=1,719

**grader:** 

**price buckets:** 50-100=137,448, 10-50=288,827, 1-10=287,037, 100-500=243,135, <1=41,445, 500-1k=69,411, 1k-5k=81,065, 5k-50k=16,663, >=50k=363

### player_comps.json

| metric | value | % |
|---|---|---|
| rows | 652,006 | 100.00 |
| unique_item_ids | 652,006 | 100.00 |
| intra_file_dupes | 0 | 0.00 |
| no_item_id | 0 | 0.00 |
| price_ok | 652,006 | 100.00 |
| price_zero | 0 | 0.00 |
| price_missing | 0 | 0.00 |
| best_offer_true | 271,294 | 41.61 |
| best_offer_missing | 0 | 0.00 |
| image_url_present | 652,006 | 100.00 |
| shipping_present | 0 | 0.00 |
| grader_present | 500,342 | 76.74 |
| grade_present | 471,215 | 72.27 |
| player_query_present | 652,006 | 100.00 |
| sold_month range | 2021-04 → 2026-06 | |
| max scraped_at | 2026-06-25T16:51:44.011578 | |

**schema variant:** v1_sold_price=652,006

**source:** ebay_player_matrix=424,530, priority_backfill=151,664, ebay_player_search=75,812

**comp_id prefix:** PL=500,342, EB=151,664

**category_v0 (keyword heuristic):** sports=525,416, pokemon=125,963, watches=365, yugioh=129, coins=110, veefriends=23

**sport_v0 (keyword heuristic, low confidence):** unknown=209,837, basketball=174,694, baseball=83,122, hockey=19,064, football=19,038, soccer=7,711, wnba=4,108, ufc_mma=2,708, wrestling=2,095, tennis=1,053, golf=1,014, racing=923, boxing=49

**grader:** PSA=413,274, BGS=35,921, SGC=29,293, CGC=21,060, BCCG=794

**price buckets:** 5k-50k=8,406, 1k-5k=46,285, 100-500=192,127, 50-100=107,222, 10-50=192,704, >=50k=101, 500-1k=49,810, 1-10=51,715, <1=3,636

### raw_player_comps.json

| metric | value | % |
|---|---|---|
| rows | 1,816,406 | 100.00 |
| unique_item_ids | 1,667,743 | 91.82 |
| intra_file_dupes | 148,663 | 8.18 |
| no_item_id | 0 | 0.00 |
| price_ok | 1,816,406 | 100.00 |
| price_zero | 0 | 0.00 |
| price_missing | 0 | 0.00 |
| best_offer_true | 929,549 | 51.18 |
| best_offer_missing | 0 | 0.00 |
| image_url_present | 1,816,406 | 100.00 |
| shipping_present | 0 | 0.00 |
| grader_present | 5,814 | 0.32 |
| grade_present | 5,275 | 0.29 |
| player_query_present | 1,816,406 | 100.00 |
| sold_month range | 2015-04 → 2026-06 | |
| max scraped_at | 2026-06-25T16:52:02.681490 | |

**schema variant:** v1_sold_price=1,816,406

**source:** ebay_player_matrix=1,785,707, ebay_player_search=30,699

**comp_id prefix:** PL=1,816,406

**category_v0 (keyword heuristic):** sports=1,553,904, pokemon=259,041, watches=1,643, coins=1,440, yugioh=254, veefriends=124

**sport_v0 (keyword heuristic, low confidence):** unknown=824,384, baseball=338,934, basketball=138,973, football=94,072, hockey=58,605, soccer=26,889, wnba=25,934, wrestling=19,148, ufc_mma=15,801, racing=6,534, tennis=2,428, golf=1,970, boxing=232

**grader:** PSA=4,972, SGC=484, BGS=248, CGC=102, BCCG=8

**price buckets:** 5k-50k=4,269, 100-500=221,570, 10-50=509,848, 500-1k=38,769, 1-10=769,905, 1k-5k=25,654, 50-100=172,647, >=50k=39, <1=73,705

## Sold-date month histogram (combined, all rows incl. dupes)

| month | rows |
|---|---|
| 2024-01 | 30 |
| 2024-02 | 32 |
| 2024-03 | 53 |
| 2024-04 | 25 |
| 2024-05 | 40 |
| 2024-06 | 74 |
| 2024-07 | 74 |
| 2024-08 | 75 |
| 2024-09 | 113 |
| 2024-10 | 51 |
| 2024-11 | 88 |
| 2024-12 | 91 |
| 2025-01 | 116 |
| 2025-02 | 74 |
| 2025-03 | 117 |
| 2025-04 | 96 |
| 2025-05 | 128 |
| 2025-06 | 132 |
| 2025-07 | 200 |
| 2025-08 | 586 |
| 2025-09 | 2,014 |
| 2025-10 | 965 |
| 2025-11 | 2,435 |
| 2025-12 | 1,684 |
| 2026-01 | 320,842 |
| 2026-02 | 543,541 |
| 2026-03 | 887,957 |
| 2026-04 | 932,923 |
| 2026-05 | 542,679 |
| 2026-06 | 396,060 |

## Scraped-at month histogram (combined) — when the store was actually fed

| month | rows |
|---|---|
| 2026-04 | 2,002,711 |
| 2026-05 | 217 |
| 2026-06 | 1,630,878 |

## Notes

- category_v0 = verbatim copy of generate_feed.detect_category (keyword heuristic; NOT real classification).
- sport_v0 = keyword heuristic over title+player_query; LOW confidence; for sizing only.
- duplicate = same eBay item id appearing more than once across the three stores (intra + cross file).
- No buyer/seller/bidder/handle data is read or emitted by this audit.
