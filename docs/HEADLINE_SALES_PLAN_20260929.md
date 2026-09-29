# MAZI as the source of truth for headline sales ($100K+, and every $1M+) — audit and plan (2026-09-29)

_Repo copy. It is also served for review from the M4 at `review/headline_sales_20260929/HEADLINE_SALES_PLAN.md`._

## What the audit found

**1. Capture has holes, source by source.** Checked against the public 2026 list of $1M+ sales: at least 20, from the
GRADEx tracker and SI.

| venue | 2026 $1M+ sales | in Neon | why the rest were missed |
|---|---|---|---|
| Fanatics Collect (Premier/Weekly auctions) | 32 all-time in their API at $1M+ (12 in 2026) | Mudkip $3.72M, LeBron SF $1.26M, Ohtani/Judge $2.16M, Jackie Robinson $1.86M | **Missing:** Flagg $8.04M, Harper $2.88M, Knueppel $2.34M, Ohtani Dynasty (Aug), Josh Allen $1.35M. **Why:** the 6-hourly refresh reads only the newest ~250 sales per category, and a Premier auction closes thousands of lots at once. The API itself has every one. |
| Goldin (weekly + Elite) | Pikachu $16.49M, Ohtani SF $2.56M, CoroCoro Pikachu $2.81M, Ohtani Bowman $2.44M | 4 of 6 | **Missing:** Exquisite LeBron $2.93M (June Elite), SGA $1.06M. **Why:** the scraper takes only the newest 4 ended auctions, once a week. |
| Private sales, brokered by Fanatics or Alt | Ohtani Logoman $11M, Ohtani SF $3.37M, Judge Dynasty $1.95M, Josh Allen NT $1.7M, Judge 2013 SF $5.2M | none | Never on a marketplace. Only press or broker announcements report them. |
| Alt (auctions) | Kobe/LeBron Logoman $3.54M, Kobe PMG $3.15M | none | Not scraped: Alt's sold history is a paid, JavaScript-only product. |
| Heritage | Jordan Masterpiece | yes (1 row) | We never scrape Heritage (its terms ban it, and it has won in court). We rely on AuctionReport and press. |
| AuctionReport (multi-house syndication) | — | 34 rows at $1M+ | **Broken since 09-13** (RSS 403). Its rows are also dirty: "Could Challenge" pre-sale estimates stored as $10M sales ×12 dates, jerseys, and duplicates of Goldin sales. |

**2. None of it reaches the front end.** Published beta sales:
- 10.6M eBay (via SCP);
- 12,951 VeeFriends eBay;
- 2,935 Whatnot;
- 141 Fanatics (VeeFriends only).

**Zero** Goldin, Heritage, Alt, REA or private sales, **zero** sports Fanatics sales, and **zero sales of $1M+** are on the
site. Also, the beta's sale `venue` check allows only whatnot, ebay, sportscardspro, goldin, fanatics and pwcc, so
Heritage, Alt, REA and private sales cannot be stored there today.

**3. Many headline cards have no MAZI ID.** Cooper Flagg has 2,292 catalog rows, and none is the 2025 Topps Chrome Update
NBA Debut Patch Auto. New ultra-premium cards need IDs minted by the spine rules.

## The plan (★ = started 2026-09-29)

### A. Capture every source automatically (Mini)
1. ★ **Fanatics: every sale of $1,000+, backfilled and kept fresh.**
   - A one-time price-band backfill across all categories from $1,000 up (304,113 sales all-time; about 6K API pages)
     is running.
   - The 6-hourly refresh now also walks "$10K+" and "$1K+", newest first, 2,500 rows deep each. A Premier close can
     no longer outrun it.
2. **Goldin: every ended auction, not just the newest 4.** Diff Goldin's auction calendar against the auction IDs we
   hold, capture each missing one, and backfill 2026's missed auctions (for example the June Elite).
3. **AuctionReport: fix the 403** by fetching through the Jina reader (as SCP already is), and **strict parsing**: only
   "sold/realized for $X" lot results for cards. No estimates ("could", "expected", "estimate"), no memorabilia, no
   repeats of the same article.
4. **Private sales and Alt: a weekly press/tracker monitor.** Sources: the GRADEx sales tracker, SI Collectibles,
   cllct, Sports Collectors Daily, Fanatics Collect and Alt press releases. It extracts every card sale of $100K+ with
   date, price, venue and card, and keeps the source link as evidence.
5. **eBay:** the signed-in sweep already walks the highest price bands first. Its $100K+ rows feed the same pipeline.

### B. One canonical record per headline sale (Neon)
- New table `headline_sales`: one row per real sale.
  - Fields: price realized (buyer's premium included), date, venue (fanatics / goldin / heritage / alt / rea / private
    / ebay / …), sale type (auction / private / fixed), card identity, grade, serial, and **all** source links as
    provenance.
  - Two reports of the same sale (venue + AuctionReport + press) merge into one record when venue, date ±3 days,
    price ±1% and card match.
- **Only exact prices.** Estimates, "could reach" and asking prices never enter.

### C. MAZI ID for every headline sale (resolve, else mint)
- **Resolve:** player_key + set + card number/code + parallel + serial + grade against the catalog. Example: Flagg
  `#DPA-CF`, 1/1, PSA 10.
- **Mint** when the card isn't catalogued, by the locked spine rule:
  `mazi:<sport>:<year>-<set-slug>:<player-slug>:<code>` (e.g. `mazi:bk:2025-topps-chrome-update:cooper-flagg:dpa-cf`,
  with `~<parallel>` when there is one).
  - Each minted ID carries its evidence (the sale title plus a checklist link).
  - It goes to the MAZIDEX lane to load into the catalog, as with VeeFriends.
- **Every sale of $1M+ gets a human check of the ID before it publishes.** A mismatch at that price is too costly;
  there are about 50 a year.

### D. Into the front end, like everything else
- An exporter `headline_sales → beta`, the same insert-only, backed-up path as VeeFriends: catalog rows for new IDs,
  and sales with `verified=false` plus provenance notes.
- **Needs Youssef:** add `heritage`, `alt`, `rea`, `private` (and other auction houses) to the beta's allowed `venue`
  values. Until then only Fanatics, Goldin and eBay headline sales can publish.
- One beta writer at a time: the exporter checks for other writers and posts to MAZIDEX first.

### E. Automation
One `headline_sales` job on the Mini, as a sources_supervisor leg, daily: capture (A2–A5) → canonicalize (B) → resolve
or mint (C) → stage for export (D). Human review happens only for new $1M+ IDs.

## Order of work
1. ★ Fanatics high-value backfill + price-floor refresh (running).
2. Goldin all-auctions fix + 2026 backfill.
3. `headline_sales` table + resolver/minter, seeded with the ~20 known 2026 $1M+ sales as the acceptance test (recall
   must reach 100%).
4. Exporter to the beta (Fanatics/Goldin/eBay first) + Youssef's venue migration for the rest.
5. AuctionReport fix + press/tracker monitor for private and Alt sales.

**Decisions for Andy:**
- (a) OK to create the `headline_sales` table in Neon.
- (b) Human check of IDs for $1M+ sales (recommended).
- (c) Ask Youssef for the venue migration.

## Status — 2026-09-29, after the first pass

- **Fanatics:** the $1,000+ backfill finished and wrote **95,607 new sales**. It then turned out the Fanatics API
  returns at most **999 rows per query**, whatever the page size (probed today). The band planner assumed 2,500, so
  **151 of its 250 price bands stopped at 999**, and about **170K sales are still unread**.
  - Fixed on the branch: bands now split to ≤ 999.
  - The 6-hourly refresh now reads five bands ($1K–1.5K, 1.5K–2.5K, 2.5K–5K, 5K–10K, $10K+), each 999 deep. That is
    4–8 days of sales per band, so a Premier close can't outrun it.
  - Re-run the backfill after the merge. Already-captured sales are skipped.
  - The 09-27 category backfills used the same wrong limit; re-run them too.
- **Goldin:** the 2026 backfill finished. **All 87 missed auctions were captured** (`goldin_comps_v2.json` went from
  48,952 to 81,467 comps).
  - The weekly leg becomes daily and captures every auction that ended in the last 30 days that isn't fully captured.
  - These sales reach Neon on the local bridge's next 12-hourly run. Its recent "failures" came only from its last
    step, a whole-table `count(*)` that timed out after every source had already committed. Fixed: it now uses an
    estimate.
- **First headline report** (floor $100K, since 2025-01-01, run before the backfills reached Neon):
  - **1,462** canonical sales from 2,162 source rows (Fanatics API 882, Neon 1,258, press seed 22).
  - MAZI ID status: resolved 403, resolved_needs_review 10, mint_candidate 579, needs_review 470.
  - Of the 22-sale 2026 $1M+ checklist, **8** are also in a feed we scrape and **14** appear only in the press
    (private, Alt or unknown venue).
  - Re-run it after the Goldin and Fanatics rows land.
- **Code:** `cardflip34/EXTERNAL-COMPS` branch `feat/headline-sales`: the headline tool + 15 tests + seed, the daily
  Goldin window, the Fanatics 999-row fix, and the bridge estimate.
