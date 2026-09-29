# Every single-card sale we're missing: audit and plan (2026-09-29)

Andy: "are there more sales even at 50k or even 1K and above we missed from these sites? put a plan in place to scrub for
any and all other single card sales we are missing … and pull them in."

**Short answer: yes, a lot.** Measured today:
- Fanatics has about **3.8M** sales we don't, about 182K of them at $1,000+.
- Goldin's calendar has **314 auctions** we never captured.
- REA's archive is roughly **150× what we hold**.
- **Heritage** we hold only from press reports.

Six more auction houses publish prices realized and aren't scraped at all.

## 1. Sources we already scrape: measured gaps

| Source | What the source has | In Neon | Why we're missing it | Fix | Status |
|---|---|---|---|---|---|
| **Fanatics Collect** (incl. old PWCC) | 5,241,526 sales. $1K+: 304,226 · $50K+: 4,945 · $100K+: 1,729 · $1M+: 32 | 1,465,383. $1K+: 122,220 · $50K+: 3,530 · $100K+: 1,404 · $1M+: 26 | (1) The API returns at most **999 rows per query**; the backfill assumed 2,500, so 151 of 250 price bands stopped early. (2) This morning's Fanatics → Neon load **failed on a table lock**, so the **95,607** new $1K+ sales captured today (incl. Flagg $8.04M, Harper, Knueppel) are on the Mini but not in Neon. | PR #2 fixes both. Then re-run the $1K+ bands. Then a full crawl at every price (~75K API pages, 2–3 days). | Fixes on PR #2 |
| **Goldin** | 490 auctions on its calendar since 2012 | 97,278 rows, **none before 2023**; only 3,381 in 2023 and 1,186 in 2024 | (1) The weekly job took only the newest 4 auctions. (2) Auctions before ~2018 carry no "Single Cards" tag, so the scraper's filter returns nothing (probed a 2017 auction: 98 lots unfiltered, 0 filtered). | 2012–2022 backfill **running**; it captures every tagged auction. Next: a title-based single-card filter for untagged auctions, then 2023–2025 behind a duplicate guard. | Running |
| ↳ Goldin duplicates | — | **2,770 rows counted twice** (2,011 at $1K+), incl. the $16.49M Pikachu | An older import ("GD-" IDs) and the current scraper both loaded the same lots, one day apart. | A guard in the Neon load so it can't recur, plus cleanup of the 2,770 (a Neon change, so your OK). | Needs Andy |
| **REA** | ~150K lots, 2004–2026 (estimate) | **969**, all $10K+ | The scraper read only the top 40 pages of a price-sorted list that stops at ~100 pages. | A crawl of each auction's own archive, singles only (sealed wax, sets, collections and memorabilia skipped; a 44-lot spot check was clean). Dry run on 2026: **1,987 lots, 1,233 single cards**. | Built, PR #2 |
| **Heritage** | Sports: $189M in 2025, weekly auctions | 152 (press reports only) | Heritage's terms ban scraping, and it has won in court: Collectrium was ordered to pay $1.76M for scraping. | Not direct. Via PSA Auction Prices Realized (Heritage's PSA slabs), a license, or skip. | **Andy decides** |
| **MySlabs** | ~150K sold (third-party figure) | 1,330 since July | Only recent sales are read. | Check whether `/browse/archive/` can be backfilled. | Next |
| **AuctionReport** | Press roundups | 398 | Broken since 09-13 (403), and dirty (estimates stored as sales). | Replaced by the direct scrapers above. | Retire |

## 2. Houses we don't scrape yet (public prices realized)

These are in priority order. All are auction archives with no login.

1. **Huggins & Scott**: ~1,400 lots per sale, since 2005. It uses **REA's URL scheme**, so the REA crawler is reused. robots.txt allows everything.
2. **Memory Lane + Lelands**: ~1,450–1,500 lots per sale, many at $100K+. They run on the same auction platform (`/Lots/Gallery`), so one parser covers both. Both return 403 to plain fetches, so it needs a slow real-browser session.
3. **Mile High + Classic Auctions**: one shared platform, plus prices-realized PDFs back to 2002. Also a browser session.
4. **Love of the Game**: 2–3 premier sales a year (e.g. a $1.34M Wagner).
5. **Clean Sweep**: monthly since 2006; some of it is already on eBay.
6. **Courtyard.io**: high-volume Pokémon slabs (each token carries the cert number and grade), from "recently sold" plus the blockchain record. Excludes Courtyard's own buybacks.

## 3. Needs your decision (paid, terms, or legal risk)

| Source | What it would add | Catch |
|---|---|---|
| Heritage | The biggest $50K+ gap | Terms ban scraping; there is a court precedent |
| PSA Auction Prices Realized | 5M+ PSA-slab sales, incl. Heritage, Memory Lane, Pristine | Cloudflare; terms unverified |
| 130point | **Real prices for eBay Best Offer sales**, which would turn our context-only rows into real prices; also Pristine | Free site; blocks its API; terms unverified |
| Card Ladder | 100M+ sales from 20+ houses | $20/mo; owned by PSA's parent. It's a license question |
| Alt | Alt's own sales since 2023 | $15/mo, login |
| Yahoo! Auctions Japan · SNKRDUNK · Mercari Japan | Japanese Pokémon volume | Mercari's terms ban access from outside its app; the others are unverified |
| SCP Auctions | Mostly memorabilia | robots.txt blocks Claude's crawler |

Skip: COMC, Sportlots, Beckett, Market Movers and PSA Vault (all on eBay or no per-sale prices), Cardmarket (no per-sale
prices), and Pristine direct (covered by aggregators).

## 4. Order of work
1. **Merge PR #2**, deploy to the Mini, and re-run the Fanatics → Neon load. That lands the 95,607 sales incl. Flagg.
   Then re-run the Fanatics $1K+ bands, then crawl the full REA archive.
2. **Goldin:** finish 2012–2022, add the title filter for untagged auctions, add the duplicate guard, backfill
   2023–2025, and clean the 2,770 duplicates (with your OK).
3. **Fanatics at full depth**, every price (multi-day).
4. **New houses:** Huggins & Scott → Memory Lane/Lelands → Mile High/Classic → Love of the Game → Clean Sweep →
   Courtyard. Each goes branch → PR → merge → run.
5. **Every new sale follows the same path:**
   - single cards only;
   - loaded into Neon by the existing bridge;
   - $100K+ sales get a MAZI ID via the headline tool;
   - sent to the site under MAZIDEX's rules.

   REA, Heritage, Alt and private sales need MAZIDEX's venue migration, which it will do with your approval.

**Decisions for Andy:** (a) merge PR #2 · (b) clean up the Goldin duplicates · (c) the Heritage route · (d) paid or
terms-limited aggregators (PSA APR, 130point, Card Ladder, Alt) · (e) the Japan sources · (f) MAZIDEX's venue
migration.
