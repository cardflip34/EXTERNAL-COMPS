# External Comps lane — status ledger 2026-08-19 (Mac Mini)

Companion to `EXTERNAL_MARKET_ENGINE_V2_AUDIT_20260819.md`. Times PDT unless marked Z.

## RUNNING (this lane)
| what | detail |
|---|---|
| nothing scraping | no eBay contact from this lane; none will start before the probe window |
| `com.mazi.deep-scrub-nightly` / `-drain` (capture-era agents, loaded) | still run on schedule, **eBay arm gated off since 11:08** (verified `ebay=None`), SCP arm continues |
| `com.mazi.priority-refresh` | capture-lane, alive — not ours |

## DONE TODAY
- Orientation + full diagnosis (audit doc §1–§5).
- **A1 gate restore** in `mazi_db/scripts/deep_scrub_runner.py` → stopped ~515 blocked eBay hits/day. Backup + note + live verification.
- **A2 eBay = BLOCKED** in `ebay_scrub_store/source_health/ebay.json`; `probe_not_before 2026-08-22T18:00Z`; probe tool ready.
- Streaming store audit (`audit/store_audit_latest.md`): 3,633,806 rows · 3,255,365 unique ids · 378,441 dupes (10.41 %) · 0 rows w/o id · price 100 % after 2-schema normalisation · OBO 38–51 % · sold-date mass Jan–Jun 2026 (eBay 90-day horizon).
- **Canonical store prototype** (DuckDB 1.5.5 + Parquet, `external_store/`): raw JSON → raw Parquet (3.86 GB → 1.18 GB, ~70 s) → `external_market_canonical_v1` **3,267,075 rows** (incl. the staged Jun-15 nightly rows: 11,710 new / 2,266 already known), **380,707 dupes removed**, 230,365 ids found by >1 source, conflicts price 1,558 / date 697 / image 912, price_missing 0, ~170 s canonical stage under a 1.4 GB cap, 1.13 GB partitioned Parquet by sold year/month, **originals untouched**. Report: `external_store/reports/migration_report_latest.json`. Model findings: multi-quantity listings need `item_id+sold_date` keys (612 ids); 15,440 "U Pick" + 30,776 lot/bundle titles must be gated out of valuation; 1,797,375 verified-price (non-OBO) rows.
- `external_engine/`: audit_stores, canonical_migrate, source_health (state machine + governor hook), ebay_gentle_probe, resource_governor (GREEN/YELLOW/RED), heartbeat (writes `heartbeat/heartbeat.json`; today says **CRIT: no store write for 1,315 h** — the detector the outage lacked). C0 tests: **10/10 pass, no network**.
- Lane repo `~/EXTERNAL-COMPS` committed (push blocked on auth). Memory notes saved for future sessions.

## BLOCKED (and why)
| item | blocker | unblock path |
|---|---|---|
| Resume 197 pending top-1000 players (pre-approved) | eBay 100 % soft-block from this egress; legacy writer needs ~6.9 GB RAM/player and writes unsafely | probe GREEN after 2026-08-22T18:00Z **and** V2 lightweight writer (item-id index + staging + fcntl writer) |
| Freshness canary / Jun-25→today hole fill | same eBay block | same; hole stays inside eBay's 90-day window only until ~Sep 23 — if the block hasn't cleared by ~Sep 1, fill it from a different egress (operator call) |
| Pull `ops/mini/` nightly system from `whatnot-sniper-m4` branch | no GitHub credentials on Mini | operator PAT/deploy key, or rsync from M4 |
| Push `~/EXTERNAL-COMPS` | no GitHub credentials on Mini | `git push -u origin main` once a token/key is present |
| Launchd-driven writes to the 6 TB volume | TCC (confirmed) | Full Disk Access for `/bin/bash` + `/usr/bin/python3` — or run V2 services from ssh-nohup |

## NEEDS OPERATOR ACTION / DECISION
1. **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access) for `/bin/bash` and `/usr/bin/python3` if V2 services are to run under launchd; otherwise I will run them from ssh-nohup like `sources_supervisor.py` did.
2. **GitHub auth** for `cardflip34/EXTERNAL-COMPS` (and read access to `whatnot-sniper-m4` if you want the Mini to pull the branch).
3. **Deep-scrub agents**: keep loaded with eBay gated (recommended — SCP history keeps flowing to trusted charts) or pause the drain until the V2 governor owns eBay. I did not change either agent.
4. **V2 eBay adapter posture**: the legacy scraper rotates user agents and sets `--disable-blink-features=AutomationControlled`. PRD §5 says no stealth; I recommend plain polite mode for V2 and will build it that way unless you say otherwise.
5. **Probe authority**: the gate note already delegates the single gentle probe to this lane after the quiet window; I plan to run it at/after 2026-08-22 18:00Z and report. Say so if you want to be asked first.
6. **Egress fallback**: if two probes are RED, decide whether the Jun–Aug hole is filled from the M4/another network (that lane's call) or waits.

## NEXT (no eBay contact required) — proceeding per PRD Phase 2
- Run ledger (`external_acquisition_ledger`, SQLite) + checkpoint/resume API.
- Canonical **writer** (single fcntl-locked writer, append-only staging → dedup vs DuckDB index → Parquet partition commit → ledger).
- Staged `ebay_nightly_2026-06-15` (13,976 rows): dedup vs canonical → merge with `PLN-<itemid>` provenance.
- Classifier v1 (beyond keywords), `external_sources_registry.json` + `EXTERNAL_SOURCE_ACCESS_MATRIX.md`, Pokémon/VeeFriends roster seeds from the 443 K / 7 K keyword rows.
- Heartbeat under a supervisor every 20 min (ssh-nohup until FDA exists).
