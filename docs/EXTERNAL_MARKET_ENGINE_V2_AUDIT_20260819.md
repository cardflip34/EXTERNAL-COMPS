# MAZI External Market Acquisition Engine V2 — Phase 1 Audit (2026-08-19)

**Lane:** External Comps (Mac Mini, `Stavross-Mac-mini`, user `stavrosaimini`)
**Auditor:** External Comps lane (Claude), read-only except where marked **ACTION TAKEN**
**PRD:** GPT master PRD + Z.ai cross-verification, received 2026-08-19 (treated as roadmap)
**Status headline:** Store integrity GOOD · eBay source **BLOCKED** (soft-block since ~Jul 1, still 100 % as of today) · legacy deep-scrub agents were *not* silent — they were hammering the blocked IP ~515×/day because the Jul-10 cooldown gate was accidentally clobbered on Jul 16 · **gate restored today** · backfill + freshness canaries are **gated on eBay clearing** · all non-eBay PRD work proceeds now.

---

## 0. Actions taken during this audit (all reversible, all backed up)

| # | Action | Why | Rollback |
|---|---|---|---|
| A1 | Restored `ebay_arm_enabled()` cooldown gate in `mazi_db/scripts/deep_scrub_runner.py` (import `os`, helper, 2 call sites). Backup: `deep_scrub_runner.py.bak.extcomps_gate_restore_20260819`. Compile OK, env gate tests 4/4, `kinds_for_job` unchanged. | The External Comps lane's own documented Jul-10 gate (`ops/DEEP_SCRUB_EBAY_GATE_NOTE.md`) was dropped by the Jul-16 SCP-ladder edit (base lacked it). Since then the deep-scrub nightly (15/day) + drain (~500/day) hit eBay 100 % blocked — zero recovery time for the IP. Both wrapper scripts still export `MAZI_DEEP_SCRUB_SKIP_EBAY=1`, so the restore re-activates the intended behaviour with no launchd change. | `cp mazi_db/scripts/deep_scrub_runner.py.bak.extcomps_gate_restore_20260819 mazi_db/scripts/deep_scrub_runner.py` |
| A2 | Wrote `source_health/ebay.json` = **BLOCKED**, `probe_not_before = 2026-08-22T18:00Z` (72 h after A1). | PRD §28/§29: record block event, pause adapter, alert operator. | `external_engine/source_health.py set ebay HEALTHY` |
| A3 | Added lane tooling under `external_engine/` (audit, source health, gentle probe). | PRD Phase 1 deliverables. | delete dir |

No plist was touched; no launchd agent was loaded/unloaded; no store file was mutated; no scrape was launched.

---

## 1. Exact current Mini state

| item | value |
|---|---|
| host / OS | `Stavross-Mac-mini.local`, macOS 15.6 (24G84), arm64 (M1 Mini) |
| uptime | 4 days (booted ~2026-08-15) |
| RAM | 16 GB total; **0.4–1.4 GB free**, swap 1.07 GB / 2 GB used, load avg 38/22/19 at 10:47 (Tailscale IPNExtension 245 % CPU, Chrome (bot fleet) 130 %, `secd` 98 %) |
| disk | `/` 51 GB free of 228 GB; `/Volumes/MAZI_EVIDENCE_6TB` 4.8 TB free of 5.5 TB (mounted, writable from ssh shells) |
| egress | direct via `en0` → 192.168.1.1; Tailscale active but **no exit node** (`ExitNodeStatus: None`); public IPv6 egress observed |
| Pythons | `/usr/bin/python3` 3.9.6 (has `ijson 3.5`, `psycopg 3.2`, `playwright`); `/opt/homebrew/bin/python3.12` (bare). **No DuckDB / pyarrow / psutil anywhere.** |
| capture-fleet processes on this Mini (NOT this lane) | `com.mazi.bot-manager` (pid 29417, `--max-bots 4`), `com.mazi.detector-select-worker` (pid 66341), `com.mazi.priority-refresh` (every 300 s), Chrome/Playwright renderers. Root-owned `/Library/LaunchDaemons/com.mazi.fleet-supervisor.plist`. Untouched. |
| external-comps processes | **none** — `pgrep -fl "player_phase\|run_player_scrub\|ebay_player_scraper"` → nothing; `sources_supervisor.py` (non-eBay multi-source loop, Jul 10–Aug 2) not running |

## 2. Repo / branch state

- `~/whatnot-sniper` on the Mini is **not a git checkout** (no `.git`). Code arrives by rsync from the M4.
- `cardflip34/whatnot-sniper-m4` branch `claude/hopeful-fermi-or1g3j`: **unreadable from the Mini** (private; no credentials: no `gh`, no credential helper, no keychain entry). Per PRD §4.5 → not blocking; working from local code.
- `cardflip34/EXTERNAL-COMPS`: readable (empty). **Push credentials absent → operator action** (PAT or deploy SSH key). Lane repo initialised locally at `~/EXTERNAL-COMPS` and committed; push queued.
- Locally present from the nightly system: `ops/mini/nightly_delta_merge.py` (Jun 16 version — **no fcntl**, clobber-safe via mtime/size re-check + `os.replace`), `ops/mini/phase_scrub_top1000_continuous.sh`, `ops/mini/nightly_1_300_post_apply_20260615.sh`, `tools/nightly_external_comps_freshness.py`, `ops/nightly_external_comps_refresh.sh`, `tools/comps_supervisor.py`, `tools/sources_supervisor.py`. **Absent locally:** `INSTALL_nightly_external_comps.md`, the TCC-safe guard, the 00:01 plist, AUDIT/ROTATION docs, scraper patch specs (M4-branch only).

## 3. Why the three launchd jobs "produced zero comps" — they didn't silently fail

| label | what it actually is | state | finding |
|---|---|---|---|
| `com.mazi.priority-refresh` | **capture-lane** job: `ops/refresh_live_priority_feed.py` (Whatnot live-stream discovery), every 300 s, RunAtLoad | alive, 778 runs, fresh output `ops/live_priority_candidates.json` 10:41 today | Not an external-comps job. Leave alone. |
| `com.mazi.deep-scrub-nightly` | Bloomberg deep-scrub (per-trusted-card 0–3 yr charts → **Neon Postgres**, not the JSON store), 03:30 daily, `ops/deep_scrub_nightly.sh` | firing daily, rc=0, own log `~/Library/Logs/mazi/launchd/deep_scrub_nightly.log` (925 KB, today 03:50) | Runs 15 cards/night. eBay leg: Jun 28–30 served rows; **Jul 4 → Aug 19: 100 % `blocked` (Error Page)**. 5 × `Alarm clock` (7200 s) timeouts early July. SCP leg works. Writes evidence to `~/mazi_local_evidence/…` (its own TCC workaround, documented in-script). |
| `com.mazi.deep-scrub-drain` | same runner, `--drain --max-cards 5`, every 600 s | firing continuously (377 runs since boot), rc=0 (263 WARN rc=1 = transient Neon "No route to host"), log 27 MB, last 10:52 today | **~500–560 cards/day, every one eBay-blocked since Jul 4** (e.g. Jul 22–Aug 18: 430–565/day, 0 rows served). ≈ **20 K blocked eBay hits from this IP in 5 weeks.** |

Root causes, in order:
1. **eBay soft-block** of the Mini egress since ~Jul 1 (HTTP 403 "Error Page" — *"Sorry / Something went wrong on our end"*, no CAPTCHA; detector `ebay_bulk_scraper.block_signature`).
2. **Cooldown gate regression**: `ops/DEEP_SCRUB_EBAY_GATE_NOTE.md` (Jul 10) gated the eBay arm; `deep_scrub_runner.py` was rewritten Jul 16 22:00 (SCP query-ladder feature) from a base without the gate → gate silently gone while the `.sh` wrappers kept exporting the env. Fixed today (A1).
3. **TCC on the 6 TB volume for launchd contexts** — confirmed, not suspected: `com.mazi.external-comps-phase-depth` (every 900 s, Jun 21–28) produced **1,198** log files in `~/Library/Logs/mazi_external_comps/` (store log dir unwritable → fallback) each reading `lock_busy … age_seconds=0` (mkdir of the lock on the volume failed). That plist and `com.mazi.external-comps-nightly-refresh` are now **unloaded**. ssh-launched processes (nohup) *can* write the volume (Remote Login grants sshd FDA); launchd GUI agents cannot until `/bin/bash` and `/usr/bin/python3` (or a wrapper app) get Full Disk Access — **operator-only** (System Settings → Privacy & Security → Full Disk Access).

## 4–5. Which legacy jobs remain / should be disabled (recommendation — no change made)

| job | recommendation |
|---|---|
| `com.mazi.priority-refresh` | keep (capture lane) |
| `com.mazi.deep-scrub-nightly` / `-drain` | **keep loaded, eBay arm gated** (now effective). They still deliver SCP history to the trusted-card charts. Fold their eBay leg under the V2 SourceGovernor later; do not add a second scheduler that also hits eBay. Operator decision: keep SCP-only, or pause drain entirely until the engine's governor exists. |
| `com.mazi.external-comps-phase-depth` (unloaded) | retire plist; superseded by V2 orchestrator (TCC-unsafe as written) |
| `com.mazi.external-comps-nightly-refresh` (unloaded) | retire plist; superseded by V2 freshness lane |
| `com.mazi.external-comps-scrub-maintainer.plist.disabled.*` | delete (already disabled) |
| `tools/sources_supervisor.py` (non-eBay loop; Fanatics/Goldin/TCGplayer/MySlabs/REA/AuctionReport → Neon) | stopped Aug 2; candidate to re-arm from ssh-nohup (FDA) as the first V2 multi-source adapters — **separate decision, not eBay** |

## 6. Current store integrity (audited today, streaming — `audit/store_audit_latest.md`)

| file | rows | unique item ids | intra-file dupes | last scraped_at | sold_date span |
|---|---|---|---|---|---|
| `ebay_comps.json` (1.20 GB) | 1,165,394 | 1,165,394 | 0 | 2026-04-21 | 2015-11 → 2026-04 |
| `player_comps.json` (0.71 GB) | 652,006 | 652,006 | 0 | 2026-06-25 | 2021-04 → 2026-06 |
| `raw_player_comps.json` (1.95 GB) | 1,816,406 | 1,667,743 | **148,663** | 2026-06-25 | 2015-04 → 2026-06 |
| **combined** | **3,633,806** | **3,255,365** | — | | |

All three parse cleanly end-to-end with ijson; 0 rows without an item id; 100 % `image_url`. Backups present: ledger `.bak_*` ×6, roster `.bak_*` ×2. No store mutation since Jun 25 (ebay_comps since Apr 21).

## 7. Duplicate audit (exact)

378,441 duplicate rows = **10.41 %** — decomposition: 148,663 intra-`raw_player_comps` + 181,073 `ebay_comps∩player_comps` (the 151,664 `priority_backfill` rows in player_comps carry `EB-` ids) + 43,103 `ebay_comps∩raw_player_comps` + 2 × 2,801 in all three. Dedup key = eBay item id (`/itm/<id>`), present on every row → canonical `EBAY:<item_id>` is safe. Cross-file dupes are *discovery* duplicates (same sale found by bulk and by player matrix) → keep one observation, accumulate `discovery_sources[]` / `queries_seen[]`.

## 8. Price completeness

- `player_comps` / `raw_player_comps`: **100 %** parseable `sold_price` > 0.
- `ebay_comps`: 702,047 rows schema-v1 (`sold_price`, bool `best_offer`, "Mar 1, 2026") + **463,347 rows schema-v2** (`price`/`price_text`, string `"True"/"False"` `best_offer`, ISO `sold_date`, explicit `item_id`, `shipping`, `source_query`, `price_range`; `source` field empty). Once both variants are normalised, price completeness ≈ **100 %**. The normaliser must handle both.
- **Best-offer share: 37.7 % / 41.6 % / 51.2 %** (ebay / player / raw). Per house rule, OBO = asking price, excluded from averages → roughly half the rows are "verified sale price" rows. Shipping present only on v2 rows. Currency assumed USD (eBay US).
- eBay sold search exposes ~90 days → sold_date mass is 2026-01 → 2026-06 (monthly: Jan 321 K, Feb 544 K, Mar 888 K, Apr 933 K, May 543 K, Jun 396 K ≤ 25th). Pre-2026 rows are a few thousand total. **Implication for Lane B (§16).**

## 9. Category audit (keyword heuristic v0 — sizing only, NOT classification)

`detect_category` copy: sports 3.08 M, pokemon **443 K** (58 K + 126 K + 259 K), coins 69 K, yugioh 34 K, veefriends **7.0 K** (6,874 + 23 + 124 — so VeeFriends is not zero), watches 2.7 K. Sport keyword v0 over sports rows: basketball 704 K, baseball 536 K, football 147 K, hockey 124 K, soccer 57 K, wrestling 53 K, WNBA 44 K, UFC 23 K, racing 12 K, golf 10 K, tennis 5 K, boxing 4 K, **unknown 1.36 M** (44 %). Real classification (PRD §35) is required; this only sizes the work.

## 10. Exact pending player count

Ledger (`player_scrub_phase_ledger.json`, 1,000 entries): `completed_existing` 799 · `completed_phase_scrub` 3 · `skipped_duplicate` 1 · `failed_retry` **1** (phase 8) · `pending` **196** (phase 9: 96, phase 10: 100). **Actionable = 197** (the brief's 809/3/206 did not sum to 1000).

## 11. Exact command to resume pending-only (DO NOT RUN until eBay is GREEN and the writer is fixed — §13)

From `ops/mini/phase_scrub_top1000_continuous.sh` (the header of `logs/phase_top1000_continuous_20260622_223931.log` is this script's banner; it ran phase 8, 69 players, Jun 22 22:39Z → Jun 23 13:25Z, rc=0):

```bash
cd /Users/stavrosaimini/whatnot-sniper
export MAZI_EBAY_SCRUB_STORE=/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store
export MAZI_EBAY_DETAIL_SPEC=0
/usr/bin/python3 -u run_player_scrub_phase.py --phase <N> --pending-only --max-per-player 10000 --sleep 15
# wrapper: nohup caffeinate -i bash ops/mini/phase_scrub_top1000_continuous.sh &   (one phase per invocation; N auto-picked = lowest phase with pending/failed_retry/running → 8, then 9, then 10)
```
`--pending-only` verified: `run_player_scrub_phase.py` L108-111 filters ledger entries with status in `pending|failed_retry|running` for that phase. Canary form: add `--limit 3`.

## 12. Nightly delta readiness

- Capture side exists: `ebay_player_scraper.py` honours `MAZI_SOLD_WINDOW=day:YYYY-MM-DD` (+`MAZI_CAPTURE_DATE`), uses recently-ended sort + early-stop, writes staged `nightly_runs/ebay_nightly_<date>.{graded,raw}.json` without touching the stores.
- Merge side exists: `ops/mini/nightly_delta_merge.py` (dry-run default, window filter, `PLN-<itemid>` comp_id, dedup by item id, clobber-safe apply). Local copy lacks fcntl (M4 branch has it).
- Staged `ebay_nightly_2026-06-15`: graded 2,658 + raw 11,318 = **13,976 rows**, all sold_date 2026-06-15, all unique item ids, 287/298 distinct player queries, no `source`/`comp_id` yet (merge stamps them). Not merged. → PRD §53: dedup against canonical (DuckDB load) then merge with provenance.
- Scheduler/guard/plist: **not on this machine** (M4 branch). The V2 plan supersedes the 00:01-only design with rolling polling + 00:01 reconciliation anyway.

## 13. Lock / writer safety status — **NOT SAFE as-is**

- `ebay_player_scraper.save_player_comps()` / `save_raw_player_comps()`: `open(path,"w")` + `json.dump` **directly on the live store** — no temp+rename, no lock → a kill mid-write truncates a 1.95 GB file. `ebay_bulk_scraper.save_comps()` has fcntl + merge (good). `nightly_delta_merge.py` is clobber-safe but unlocked.
- The per-player subprocess **loads both stores into RAM: measured 286 MB / 100 K rows ⇒ ≈ 5.1 GB (raw) + 1.8 GB (graded) ≈ 6.9 GB per player** on a Mini with 0.4–1.4 GB free while the fleet runs → swap storm (the PRD's "M4 swap problem"). Rewrites ~2.7 GB of JSON after every player.
- Runner lock: mkdir-based `.phase_top1000_continuous.lock` (stale-recovery 1800 s) — fine for one phase writer, not a store lock.
- **Verdict:** the legacy backfill must not be resumed through this writer. Resume through the V2 path: item-id index (SQLite/DuckDB, ~300 MB) for dedup + append-only staging + ONE canonical writer with fcntl. This is PRD Phase 1–2 and is a prerequisite for Phase 5, eBay aside.

## 14. Recommended canonical storage architecture (to benchmark, Phase 2)

Raw immutable (`external_store/raw/` — original JSON + nightly run files, never rewritten) → **Parquet** partitions (`external_store/parquet/source=ebay/year=YYYY/month=MM/`) → **local DuckDB** (analytics, dedup index, ledger views) → production Neon Postgres for trusted observations. Benchmark DuckDB `read_json` over the three stores with bounded memory (`SET memory_limit='2GB'`), dedup by item id, write Parquet; measure wall time, peak RSS, Parquet size, query latency. SQLite only for the tiny hot state (ledger, source health, checkpoints). Decision after benchmark — not before.

## 15–17. Freshness / backfill architecture / scheduling (proposal)

- **Lane A freshness:** rolling bounded polling of the completed-ranks-first universe (PRD §10) + 00:01 reconciliation of the previous calendar day + T+1/T+3/T+7 re-reconciliation, all through the SourceGovernor; heartbeat every 20 min; freshness SLO warn 6 h / crit 24 h.
- **Lane B backfill:** newest-missing-first. **Hard source fact:** eBay sold search only returns ~90 days, so Lane B on eBay = (a) the Jun 25 → today hole (still within 90 d for ~5 more weeks — time-critical), (b) the 197 pending top-1000 players, (c) widening the universe. History older than 90 days must come from other adapters (Goldin/Heritage/PWCC/REA/Fanatics archives, SCP, 130point, Terapeak if licensed) — see source matrix.
- **Scheduling:** one `com.mazi.external-acquisition-orchestrator` + one `com.mazi.external-supervisor` (heartbeat/self-heal). Until FDA is granted, launchd agents must write under `$HOME` and a *staging→writer* hop (ssh-nohup or FDA-granted binary) owns the volume — or the orchestrator runs from an ssh-nohup keep-alive like `sources_supervisor.py` did.

## 18. Resource limits (Mini)

GREEN: free ≥ 2 GB & swap-used < 1 GB & load < 12 → full plan. YELLOW: free < 2 GB or swap ≥ 1 GB → Lane B paused, Lane A 1 worker. RED: free < 0.75 GB or swap ≥ 1.8 GB or load > 24 → all lanes paused, alert. **Today = RED/YELLOW boundary.** Max 1 Playwright browser per lane, session max age 30 min / 200 pages, hard cap on renderer count.

## 19. Source access matrix — initial (`docs/EXTERNAL_SOURCE_ACCESS_MATRIX.md` to follow)

Already-built adapters on this Mini: eBay (Playwright; **BLOCKED**), Fanatics (catalog v3 + refresh leg; Neon bridge), Goldin v2 (weekly), TCGplayer sold, MySlabs, REA, AuctionReport (RSS), SportsCardsPro/SCP (deep-scrub SCP arm; politeness lock). To audit: Heritage, PWCC/Fanatics Collect, 130point, CardLadder, Alt, PSA/BGS APR, Terapeak (licensed), Whatnot (proprietary, via M4).

## 20. eBay health / rate strategy

State **BLOCKED** (`source_health/ebay.json`). Protocol: zero contact ≥ 72 h (from 2026-08-19 17:55Z) → one gentle probe (`external_engine/ebay_gentle_probe.py`, 1 request, production path, never retries) → if GREEN: DEGRADED state, resume at ≤ 1 request / 20–30 s with randomised delays, single session, bounded pagination, query cooldowns, auto-pause on any 403/429/challenge; if RED: stay BLOCKED, next probe +72 h; escalate to operator (egress/IP discussion) after 2 RED probes. Existing scraper uses UA rotation + `--disable-blink-features=AutomationControlled` — predates PRD §5; **operator decision** whether V2 keeps those (PRD says no stealth; I recommend plain polite mode).

## 21–25. Expansion / triggers (design only — after freshness is live)

Sports tiers T2 2,500 / T3 5,000 / long tail; set/number/parallel/insert/grade/serial templates via token-set variant engine; Pokémon `pokemon_roster.json` (443 K keyword rows already exist as seed); VeeFriends discovery audit (7 K rows exist as seed); card-library coverage score → targeted jobs; Whatnot trusted capture → weak-coverage trigger.

## 26–27. 8504 ingestion / MVE

RAW → DEDUP → CLASSIFY → PRICE GATE (OBO excluded from averages, flagged) → IMAGE GATE → IDENTITY → CONFIDENCE → mazidex-admin / 8504 trusted ingestion; MVE reads canonical trusted only (never raw JSON).

## 28. Monitoring plan

Heartbeat file every 20 min (orchestrator/workers/writer alive, drive mounted, locks fresh, source states, last successful query, last canonical write, RAM/swap/disk) + status page; alerts per PRD §47. "Loaded-but-dead" check = compare `launchctl print` runs/last-exit with heartbeat age.

## 29. Canary plan

C0 (no network): normaliser + dedup + ledger unit tests. C1 eBay probe GREEN. C2 freshness canary: 5–10 bounded searches for one day window, twice → zero new dupes on rerun, prices parse, images present, RAM flat, ledger complete. C3 backfill canary: `--limit 3` pending players via V2 writer. Only then C4 cohort + hole fill.

## 30. Exact implementation sequence (revised for reality)

1. ✅ A1 gate restore → zero eBay contact. 2. ✅ eBay BLOCKED recorded, probe scheduled ≥ 2026-08-22T18:00Z. 3. ✅ Streaming audit. 4. Lane venv + DuckDB benchmark + canonical dedup migration prototype (non-destructive). 5. Ledger + source-health + resource governor + heartbeat. 6. Unit tests (C0). 7. Operator: FDA grants, EXTERNAL-COMPS push auth, decision on deep-scrub SCP-only vs pause. 8. Probe → if GREEN: C2 freshness canary (hole fill first: Jun 25 → today is still inside eBay's 90-day window for ~5 weeks) → C3 backfill canary → cohort. 9. Multi-source adapters re-armed under the governor. 10. Expansion.

---
*Generated 2026-08-19 by the External Comps lane. Companion data: `ebay_scrub_store/audit/store_audit_latest.{json,md}`, `ebay_scrub_store/source_health/ebay.json`.*
