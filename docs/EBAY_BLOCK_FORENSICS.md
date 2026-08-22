# Why eBay scraping worked before June and doesn't now — forensics (2026-08-22)

## The timeline (evidence, not inference)

| date | what the data shows |
|---|---|
| Apr 16 – Jun 23 | Heavy scraping. **36 active days, 4,264,776 rows**, avg **118,466/day**, peak **489,202 rows in one day** (Jun 19–20) |
| **Jun 23** | Last phase scrub completes successfully (`rc=0`) |
| **Jun 25** | Last eBay row ever captured (5,544 rows) — heavy scraping had already STOPPED |
| Jun 28 – Jul 1 | Deep-scrub agents still hitting eBay at *low* volume — **served normally** (Jun 28: 26/28 cards served; Jul 1: 5/6 served) |
| Jul 2 | `scrub_2026_backfill.py` edited (date-window logic) + a pure-logic unit test written. **Never run against eBay** — no logs, zero rows landed |
| **Jul 4** | **100% blocked.** Drain: 50/50 blocked. Nightly: 15/15 blocked. Zero served |
| Jul 4 – Aug 19 | Gate regression: ~515 blocked requests/day for 6 weeks (~20,000+ total) |
| Aug 21 | Signed-in human browser from the same IP → **served normally (200, 59 items)** |

## What we did NOT find

- **No volume spike before the block.** We had *stopped* heavy scraping 9 days earlier. The block landed while traffic was at its lowest.
- **No code change that increased load.** The Jul 2 edit was logic-only and never executed.
- **No reckless pacing.** Our scrapers used **2–4.5 s randomized delays** between requests — inside the commonly cited tolerance of ~1 request per 2–4 s per IP.
- **Not an IP ban.** Proven Aug 21: the same IP serves a verified human browser fine.

## What most likely happened (inference — stated as such)

The flip was **binary and overnight**: 100% served on Jul 1, 100% blocked on Jul 4, with nothing in between. Gradual
rate-limiting produces a ramp; this produced a cliff. A cliff is characteristic of a **classification decision** —
an identity being added to a "known automation" set — rather than throttling.

Two explanations fit the evidence, and from outside we cannot distinguish them:

1. **Cumulative reputation scoring with lagged enforcement.** eBay scores traffic over a rolling window. Our
   Apr–Jun footprint — millions of automated sold-searches from a single residential IP — accumulated until it
   crossed a threshold. Enforcement landed after the activity, not during it. This explains why the block
   appeared *after* we stopped.
2. **A detection change on eBay's side around Jul 1–4** that our accumulated history matched immediately.

Either way, the operative fact is the same: **"it worked before June" doesn't mean the approach was sustainable —
it means we were accruing reputation debt that hadn't been called in yet.** The scraping wasn't fine-then-broken;
it was always heading here at that volume.

## What made it unrecoverable (this part was ours, and avoidable)

The cooldown gate added Jul 10 was silently deleted by a Jul 16 refactor. From Jul 17 to Aug 19 the deep-scrub
agents hit the blocked endpoint **~515 times a day — over 20,000 blocked requests.** Any decay timer or cooldown
window was continuously reset. We spent six weeks proving to eBay's systems, daily, that this IP runs automation.

That is the single clearest lesson: **a block is not a thing to keep testing.** It is a thing to stop touching.
The gate is restored (`grep -c ebay_arm_enabled` = 3) and the watchdog now verifies *progress*, not just liveness.

## What this means going forward

- **Resuming the same approach reproduces the same outcome**, on any IP, on any account — just on a delay. At
  ~118K rows/day sustained, we would rebuild the same reputation debt.
- The wall we now hit is **human-verification**, not a ban. Clearing it programmatically is the bot-protection
  bypass the project rules (and eBay's ToS) prohibit.
- The durable answer is a **sanctioned channel** (Marketplace Insights API — limited release, restrictive) or
  **licensed data**. Note the API is gatekept; approval is not a formality.
- **We already routed around the loss:** SCP delivers 6.49M sports sales through today (99.99% disjoint from our
  eBay corpus — a different, larger dataset, not a substitute), and TCGplayer covers Pokémon/TCG.

## The honest bottom line

Nothing "broke." We ran a residential IP at industrial scrape volume for two months, eBay's systems eventually
classified it, and then we spent six more weeks confirming that classification for them. The data we lost access
to has largely been replaced from sources that don't require fighting an anti-bot system.
