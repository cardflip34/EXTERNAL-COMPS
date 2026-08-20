# Do we need eBay, or does SCP already have the same ~90-day sales? — verdict (2026-08-19)

**Short answer: SCP already covers the eBay gap *for sports*, but NOT for non-sports, and NOT with item-level data. eBay is still needed — for a narrower, well-defined job.**

## The numbers (Neon `external_transactions`, queried 2026-08-19)

| source | rows | sold_date span | last 90 d | since 2026-07-01 |
|---|---|---|---|---|
| sportscardspro | 5,149,085 | 2020-04 → **2026-08-18** | 471,107 | 433,875 |
| ebay | 3,139,694 | 2015-04 → **2026-06-30** | 587,713 | **0** |
| fanatics | 1,401,355 | 2009 → 2026-08-01 | — | — |
| goldin | 57,343 | 2023 → 2026-07-31 | — | — |
| tcgplayer | 53,187 | 2026-07-07 → 2026-08-19 | — | — |

eBay stops **dead at 2026-06-30** (the soft-block). SCP picks up exactly there: 2026-07 = **370,777** sales, 2026-08 = 63,098 (partial). So the "8-week hole" is, for the most part, **already filled — by SCP.**

## Why SCP substitutes for eBay *on sports* (but is not "the same data")
SportsCardsPro's sold prices are themselves **derived from eBay sold listings**. So for a sports card, an SCP July sale is, in effect, the same eBay transaction — routed through SCP. That's why SCP's July volume (370K) is in the same league as eBay's typical monthly sports volume. For **sports price history in the gap window, we do not need to re-scrape eBay.**

## Why eBay is still required — three gaps SCP cannot fill

1. **Non-sports categories — SCP has essentially none.** Of SCP's 433,875 rows since Jul-01: Pokémon ~69, other TCG **0**, VeeFriends **0**, coins ~6. But eBay's firehose was **~40 % non-sports** — Pokémon alone was 483K rows in the canonical store, plus TCG, coins, VeeFriends, watches. **SportsCardsPro is a sports-only site; it can never provide these.** For Pokémon/TCG/VeeFriends/coins, eBay (or TCGplayer for TCG) is the only game in town.

2. **No item-level discovery data.** SCP rows since Jul-01: **0 eBay item URLs, 0 images.** SCP gives you `(card, grade, price, date)` aggregated per card — not the individual listing. eBay gives `item_id` (the dedup key), the **listing photo** (front-end card image, identity evidence), the **best-offer flag** (the "verified sale price" differentiator), seller, shipping. None of that survives the SCP round-trip.

3. **Freshness resilience & breadth.** SCP's own broad backfill silently died on the **Aug-15 reboot** (see below); its per-card grain also can't discover *new* cards the way an eBay search sweep does.

## Verdict
- **eBay backfill of the Jun 25 – Aug sports hole → LOW priority.** SCP already holds it.
- **eBay is REQUIRED for:** (a) non-sports categories (Pokémon/TCG/coins/VeeFriends/watches), (b) item-level metadata (item_id, image, OBO) that feeds dedup + the front-end + the "verified sale price" signal, (c) freshness/discovery breadth.
- So: **run the Aug-22 probe as planned**, but when eBay resumes, point the freshness lane first at **non-sports + high-value item-level capture**, not at re-collecting sports prices SCP already has. This also means we can resume eBay at **lower volume** (polite mode is a better fit than a full firehose).

## Operational finding fixed today
The **SCP broad scrub** (the "millions of comps" sports-history engine: 7.6M rows in `~/mazi_scp_broad/scp_broad_comps.jsonl`, catalog-wide) **stopped on the 2026-08-15 reboot** — its `nohup` driver + Neon bridge died and its watchdog launchd agent was **not loaded**, so nothing restarted them (same silent-death class as the deep-scrub gate). That's why SCP's August volume (63K) is far below July (370K). **Fixed 2026-08-19:** ran the watchdog (driver + scrubber + bridge all back up, immediately parsing sales) and wired the watchdog into the V2 supervisor as a **healer** (runs every ~5 min, governed) so a reboot can no longer silently kill it. The deep-scrub SCP arm (trusted ~1.3K cards) was unaffected and stayed fresh (scraped_at 2026-08-19).

*Data source: `external_transactions` on Neon; local `~/mazi_scp_broad/`. No buyer/seller data in this doc.*
