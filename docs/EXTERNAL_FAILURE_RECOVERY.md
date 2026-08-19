# External Comps V2 — failure recovery runbook (Mac Mini)

_Last updated 2026-08-19 by the External Comps lane. Plain-language; every command is copy-pasteable from an ssh
session on the Mini (`ssh mini`, user `stavrosaimini`)._

## 1. How the "can't silently stop" loop works

```
launchd  com.mazi.extcomps-keepalive  (every 600 s, RunAtLoad)
   └─ ops/extcomps_keepalive.sh       checks PID file + kill -0; if the supervisor is down →
        └─ ssh localhost              (key auth; sshd-spawned processes inherit Remote Login's Full Disk Access)
             └─ nohup /usr/bin/python3 external_engine/supervisor.py   (single instance, fcntl lock)
                  ├─ resource governor every 60 s   (GREEN/YELLOW/RED)
                  ├─ heartbeat every 20 min         → ebay_scrub_store/heartbeat/heartbeat.json (+ history)
                  ├─ eBay probe scheduler           → runs external_engine/ebay_gentle_probe.py ONCE when
                  │                                   source_health/ebay.json says BLOCKED/COOLDOWN and
                  │                                   now ≥ probe_not_before (and ≥72 h since the last probe)
                  └─ (lanes attach here later: freshness / backfill / targeted — gated by source_health + governor)
```
Why the ssh hop: a launchd agent on this Mini cannot read or write `/Volumes/MAZI_EVIDENCE_6TB` (TCC; verified
2026-08-19 — even `ls` fails). sshd can. Until Full Disk Access is granted to `/bin/bash` + `/usr/bin/python3`,
anything that must touch the volume runs through `ssh localhost`.

Files: supervisor log `~/Library/Logs/mazi_external_comps/extcomps_supervisor.out`; keep-alive log
`~/Library/Logs/mazi/launchd/extcomps_keepalive.log`; state `ebay_scrub_store/heartbeat/supervisor_state.json`;
PID/lock `~/mazi_local_evidence/extcomps_supervisor.lock`.

## 2. Quick health check (30 seconds)
```bash
cd ~/whatnot-sniper && /usr/bin/python3 external_engine/heartbeat.py
```
Reads: `OK | WARN | CRIT`, resource level, eBay state, hours since the last store write, launchd states.
Then `cat /Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store/heartbeat/supervisor_state.json` — `updated_at`
should be < 2 minutes old, `next_probe_at` shows when the eBay probe will run.

## 3. Symptoms → actions
| symptom | meaning | action |
|---|---|---|
| heartbeat `CRIT: no store write for N h` (N > 24) | nothing has written canonical/legacy stores | is the supervisor alive? are lanes gated (eBay BLOCKED)? see §4 |
| heartbeat `CRIT: resource RED` | RAM/swap/load/disk/browsers over the red line | lanes pause automatically; check `ps`, Chrome/Playwright count; the capture fleet usually causes load |
| `supervisor_state.json` stale (> 5 min) | supervisor dead and keep-alive not relaunching | `tail ~/Library/Logs/mazi/launchd/extcomps_keepalive.log`; test `ssh -o BatchMode=yes localhost true`; if ssh fails, fix key/Remote Login; manual start: `bash ops/extcomps_keepalive.sh` |
| `source_health/ebay.json` state BLOCKED after a probe | eBay still soft-blocking this egress | wait for the next `probe_not_before` (+72 h). Two RED probes → raise the egress/IP question with the operator |
| `source_health/ebay.json` state DEGRADED, event `operator_attention` GREEN probe | block cleared | resume **politely** (≤1 req / 20–30 s, single session, bounded pages): C2 freshness canary first, then backfill canary (`--limit 3`), both through the canonical writer |
| `ledger.py incomplete` lists runs in `started/running/retry` | a worker crashed mid-run | resume from its `checkpoint` JSON; never mark complete by hand |
| writer `WRITER BUSY` (rc 3) | another ingest holds `external_store/.writer.lock` | wait; if the lock owner PID is dead, delete the lock file |
| deep-scrub drain log shows `blocked_blocked` again | the eBay gate got lost (again) | `grep -c ebay_arm_enabled mazi_db/scripts/deep_scrub_runner.py` must be 3; restore from `deep_scrub_runner.py.bak.extcomps_gate_restore_20260819` pattern; see `ops/DEEP_SCRUB_EBAY_GATE_NOTE.md` |

## 4. Manual controls
```bash
# stop / start the supervisor (keep-alive will restart it within 10 min unless you bootout the agent)
pkill -f external_engine/supervisor.py                      # stops after its current 60 s tick
bash ~/whatnot-sniper/ops/extcomps_keepalive.sh             # start now (via ssh localhost)
# disable the whole loop (rollback)
launchctl bootout gui/$(id -u)/com.mazi.extcomps-keepalive; rm ~/Library/LaunchAgents/com.mazi.extcomps-keepalive.plist
pkill -f external_engine/supervisor.py
# eBay state by hand
/usr/bin/python3 external_engine/source_health.py show ebay
/usr/bin/python3 external_engine/source_health.py set ebay COOLDOWN --note "..." --probe-not-before 2026-08-25T18:00:00+00:00
/usr/bin/python3 external_engine/ebay_gentle_probe.py --dry-run    # URL only; add --force to probe now (1 request)
# canonical writer
.venv_extcomps/bin/python external_engine/canonical_writer.py stats
.venv_extcomps/bin/python external_engine/canonical_writer.py ingest --in staged.json --source ebay_player_matrix --lane freshness --dry-run
# ledger
.venv_extcomps/bin/python external_engine/ledger.py summary --hours 24
.venv_extcomps/bin/python external_engine/ledger.py incomplete
```

## 5. Rebuilds
- Canonical store from raw (one-time bootstrap; do NOT re-run casually once the writer has ingested new rows — it
  rebuilds only the migration's base files; `ingest_*` files survive but the index must be rebuilt):
  `.venv_extcomps/bin/python external_engine/canonical_migrate.py --skip-raw-if-exists --memory-limit 1400MB --threads 1`
  then `.venv_extcomps/bin/python external_engine/canonical_writer.py rebuild-index`.
- Store audit (aggregates only): `/usr/bin/python3 external_engine/audit_stores.py`.

## 6. What is NOT automated yet (as of 2026-08-19)
eBay freshness/backfill lanes (gated on the probe), classifier v1, card-identity mapping, 8504 ingestion,
multi-source adapters under the governor (Fanatics/Goldin/TCGplayer/MySlabs/REA exist as scripts; not re-armed).
