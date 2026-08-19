# External source access matrix — v1 (2026-08-19)

Machine-readable twin: `external_engine/external_sources_registry.json`. "verify" = assumption to confirm before scaling.
Rule of the road (PRD §5/§28): prefer API → licensed export → permitted public feed → documented browser-accessible
sold history → compliant low-frequency browser acquisition. Never bypass CAPTCHAs, logins, bot protection; on 403/429/challenge → pause, record, alert.

| source | covers | sold depth | adapter on Mini | status today | next step |
|---|---|---|---|---|---|
| **eBay** | sports, pokemon, tcg, vee, coins, watches | ~90 d | `ebay_player_scraper.py` (Playwright) | **BLOCKED** (soft-block since Jul 1; probe ≥ Aug 22 18:00Z) | polite adapter (no UA rotation / automation flag), staged → writer; Marketplace Insights / Terapeak = verify |
| Fanatics Collect (incl. ex-PWCC) | sports, pokemon, tcg | years | `fanatics_full_catalog_scraper_v3.py` (catalog done) + `fanatics_recent_refresh.py` | **ACTIVE** (sources lane, 6 h) | canonicalise (source=fanatics) into the canonical store |
| Goldin | sports, pokemon, memorabilia | years | `goldin_scraper_v2.py` weekly | **ACTIVE** (sources lane, Fri) | canonicalise |
| TCGplayer | pokemon, tcg | ~1 y | `tcgplayer_sold_scraper.py` | **ACTIVE** (sources lane, 12 h) | partner API eligibility = verify; canonicalise |
| MySlabs | sports, pokemon (graded) | ~1 y | `myslabs_scraper_v2.py` | **ACTIVE** (6 h) | canonicalise |
| REA | vintage sports | decades | `rea_scraper.py --pages 40` | **ACTIVE** (24 h) | canonicalise |
| AuctionReport (RSS) | multi-house top lots | ~1 y | `auctionreport_scraper.py` | **ACTIVE** (6 h; wrote today) | canonicalise |
| SportsCardsPro | sports | ~3 y | deep-scrub SCP arm (politeness lock) | **ACTIVE** (trusted-card charts) | keep; respect Cloudflare challenge = stop |
| Heritage | sports, comics, coins | decades | `heritage_scraper.py` | exists, unscheduled (Apr 12) | verify archive access rules → schedule weekly |
| COMC | sports, tcg | ~1 y | `comc_scraper.py` | exists, unscheduled | verify → schedule |
| Pristine Auction | sports | years | `pristine_scraper.py` | exists, unscheduled | verify → schedule |
| StarStock | sports | ~1 y | `starstock_scraper.py` | exists, unscheduled | low priority |
| 130point | sports, tcg (eBay OBO accepted prices) | ~90 d | none | not built | high value (true OBO prices); verify ToS first |
| PSA APR | sports, tcg (multi-house realized) | decades | none | not built | verify ToS; good long-history source |
| Card Ladder | sports, tcg | years | none | not built | licensing decision |
| Whatnot (MAZI live) | all | MAZI-owned | M4 fleet | out of this lane's scope | consume for Whatnot-vs-external spread |

## Why this matters for Lane B
eBay sold search only reaches ~90 days, so **all history older than 90 days must come from the rows above** (Goldin/REA/Heritage/Fanatics archives, PSA APR) — that is the real "walk back to 2019" path. The Jun 25 → Aug 19 eBay hole is recoverable from eBay itself only until ~Sep 23.

## Fees / price semantics to normalise
auction houses: realized price may or may not include buyer's premium (record `buyer_premium` + `all_in_price`); eBay: `best_offer` rows are asking prices (gate = `obo`); marketplaces (TCGplayer/MySlabs/COMC): net price, no premium.
