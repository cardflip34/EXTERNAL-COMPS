# eBay activation runbook — how MAZI gets eBay sold data (2026-08-21)

## Corrected diagnosis (this is the important part)

Earlier we concluded "the home IP is blocked." **That was wrong, and we proved it tonight.**

| test from the SAME home network | result |
|---|---|
| automated / unauthenticated request | **HTTP 403** "Something went wrong on our end" |
| headless browser | **403** |
| headed browser, not signed in | **HTTP 200** but "Please verify yourself" wall, 0 results |
| **headed browser, operator signed in + verified** | **HTTP 200 · SERVED · 59 results** ✅ |

**Conclusion: this is bot/automation detection, not an IP ban.** The network is fine. eBay serves a real
authenticated human browser from this exact IP.

**Why this is good news:** it means the **official eBay API will work from this network with no proxy, no VPS,
no egress change.** The transport problem we thought we had does not exist. We just need a sanctioned channel.

## Why we don't point the scraper at the signed-in session

eBay's User Agreement prohibits robots/spiders/scrapers/data-mining tools against the site without written
permission — that's what the verify wall enforces. Running the 90-day backfill through an authenticated
session doesn't remove that; it **attaches it to the account**. The risk profile changes from "an IP gets
throttled" (recoverable, costs nothing) to "**the eBay account gets suspended**" — a real, hard-to-reverse
loss for a business that buys and sells cards. And practically: eBay flagged this network *because* of ~515
automated hits/day, so resuming automation would re-trigger the wall quickly, now with an account attached.

The data isn't worth that trade, because we already have most of it (SCP covers sports, TCGplayer covers
Pokémon/TCG for the current window) — see `SCP_VS_EBAY_COVERAGE_20260819.md`.

## Path A — Terapeak — **NOT a viable scrub source** (corrected 2026-08-21)

eBay's own sold-data research tool: free with a seller account, **3 years** of sold history, and it shows the
**real accepted price on Best Offer sales** (which public search hides). Genuinely valuable data.

**But it has no bulk export.** Multiple eBay seller-community threads asking how to export Terapeak data to
CSV/Excel are answered with: you can't. There are also **daily request limits** that sellers report as
restrictive. The operator's recollection was correct — the export button in Seller Hub is for *your own active
listings*, not for Terapeak's sold research.

**Verdict: Terapeak is a human lookup tool, not a data pipeline.** It is excellent for pricing one card by hand.
It cannot feed a comps database of millions of rows. Getting 3 years of data out of it would mean either manual
copy/paste (absurd at our scale) or scraping the Terapeak UI — which is the same ToS problem as before, made
worse because it sits behind an authenticated account.

**Do not build the acquisition engine on Terapeak.** Use it manually when you personally want to sanity-check a
card's value — particularly a Best-Offer sale, where it's the only place the true price is visible.

*Open question worth 5 minutes of eyeballs: does Terapeak show INDIVIDUAL listings (with item numbers) or only
aggregated product-level stats (avg price, sell-through)? If aggregate-only, it is doubly unusable for a comps
store, which needs one row per sale.*

## Path B — Marketplace Insights API (the durable, programmatic fix)

eBay's sanctioned API for **sold/completed** items (~90 days). This is what the daily cron should run on
permanently: no walls, no blocks, no ToS risk, stable schema.

1. **developer.ebay.com** → *Join the eBay Developers Program* (sign in with the eBay account you just used)
2. Create an **application keyset** (Sandbox for testing, Production for live) → gives App ID / Cert ID
3. Implement **OAuth client-credentials** to mint an application access token
4. **Marketplace Insights API is limited-release — you must apply for access.** In the application, describe
   MAZI accurately: a card-market analytics platform building sold-price comparables; state expected call
   volume and that data is for internal valuation, not redistribution.
5. While waiting, the **Browse API** (generally available) is useful for item/catalog metadata, though it
   covers active listings rather than sold history.

**What I need from you when approved:** App ID + Cert ID in `.env.external_comps_bridge` as
`EBAY_APP_ID` / `EBAY_CERT_ID`. Nothing else — I'll wire the client, token refresh, and the lanes.

## What's already built and waiting (transport-agnostic)

The acquisition engine is **done and tested** — only the eBay doorway is missing. It doesn't care which path
above supplies rows:

| component | status |
|---|---|
| `canonical_writer.py` — fcntl single writer, `(item_id, sold_date)` dedup, Parquet partitions | ✅ tested, running |
| `ledger.py` — run ledger, checkpoints, `window_done()` so completed days never re-scrape | ✅ tested |
| `classifier_v1.py` — category + sport with confidence over all sources | ✅ running (4.4M rows) |
| block/challenge auto-detection | ✅ **fixed 2026-08-21** — now catches "verify yourself" walls (previously only "robot"), so empty challenge pages can never be logged as real data |
| 90-day recent-first backfill lane (day windows, price-band subdivision, resume) | ready to wire to transport |
| daily 24h reconcile + T+1/T+3 late-index passes, as a governed supervisor lane | ready to wire to transport |

**Day-one after either path:** backfill runs newest→oldest across 90 days, each `(query, day)` a ledger job that
never repeats; the daily lane reconciles the previous calendar day at 00:01 with T+1/T+3 re-passes for
late-indexed sales; everything dedups by item+date, so re-runs are free.

## Current eBay state

`source_health/ebay.json` = **BLOCKED** (correctly — for *automated* access). The automated probe is disabled;
no scraper will touch eBay while this stands. State flips when a sanctioned transport is in place, not before.
