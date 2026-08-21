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

## Path A — Terapeak (immediate, free, no code)  ← try this first, you're already signed in

eBay's own sold-data research tool, included with an eBay **seller** account at no extra cost, in **Seller Hub →
Research**. Gives sold/completed listing history (materially deeper than the ~90 days the public search shows)
with filters and CSV export.

1. While signed in: **Seller Hub → Research → Terapeak Product Research**
2. Search e.g. `cards`, set the date range and category, filter to **Sold**
3. **Export CSV**

**Why this matters:** a Terapeak CSV export drops straight into the engine we already built — `canonical_writer.py`
ingests staged rows, dedups on `(item_id, sold_date)`, and writes canonical Parquet. If the export carries item
ids, dates and prices, we get the historical archive **today**, legitimately, with zero scraping. Tell me the
column headers of an export and I'll have the ingester ready.

*Check eligibility in Seller Hub — availability/limits depend on account type and can change.*

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
