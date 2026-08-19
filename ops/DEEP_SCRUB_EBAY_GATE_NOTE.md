# Deep-scrub eBay arm: COOLDOWN GATE (added by External Comps lane, 2026-07-10)

WHAT: `MAZI_DEEP_SCRUB_SKIP_EBAY=1` (exported in ops/deep_scrub_nightly.sh L62 and
ops/deep_scrub_drain.sh L50) skips the eBay leg of every deep-scrub run via
`ebay_arm_enabled()` in mazi_db/scripts/deep_scrub_runner.py (+2 call-site lines).
SCP is UNTOUCHED and keeps covering 0-90d via MAZI_SCP_EBAY_BOUNDARY_DAYS=0.

WHY: eBay has soft-blocked Mini since ~Jul 1 ("Error Page", no captcha). Deep-scrub
was making ~100+ blocked eBay hits/day (nightly ~15 + drain up to 135), preventing
the block from ever clearing. Zero-contact cooldown is the recovery bet.

WHO/WHEN REVERTS: External Comps runs a single gentle probe after >=72h quiet; on a
GREEN probe it will coordinate. To resume eBay: delete the two export lines (or set
=0) — takes effect next run, no restart, no code revert needed.

BACKUPS: deep_scrub_runner.py.bak.extcomps_gate_20260710, both .sh.bak.extcomps_gate_20260710.
Gate tests passed 5/5 on deploy (env on/off/non-1 + kinds_for_job unchanged).

---
## 2026-08-19 UPDATE — gate was silently lost Jul 16, RESTORED today (External Comps lane)

WHAT HAPPENED: `mazi_db/scripts/deep_scrub_runner.py` was rewritten 2026-07-16 22:00 (SCP query-ladder /
non-sport-skip feature) from a base that predated this gate. `ebay_arm_enabled()` + its 2 call sites vanished
while both `.sh` wrappers kept exporting `MAZI_DEEP_SCRUB_SKIP_EBAY=1` (no effect). Result: nightly 15/day +
drain ~500/day eBay hits, 100 % `blocked` (Error Page), Jul 17 → Aug 19 — the soft-block never got quiet time.
Last version WITH the gate: `deep_scrub_runner.py.bak.20260716T220049Z`.

RESTORE: 2026-08-19 10:55 PDT — `import os`, `ebay_arm_enabled()`, `run_ebay = run_ebay and ebay_arm_enabled()`
(drain), `run_ebay=ebay_arm_enabled()` (nightly). Backup: `deep_scrub_runner.py.bak.extcomps_gate_restore_20260819`.
py_compile OK; env tests 4/4 ('1'→False, '0'/unset/'true'→True); `kinds_for_job` unchanged.
VERIFIED LIVE: drain cycle 11:08 PDT (deep_scrub_20260819T180824Z): 5/5 jobs `ebay=None`, SCP resolving.

ZERO-CONTACT CLOCK: starts 2026-08-19 17:52Z (last blocked hit). Source state: `ebay_scrub_store/source_health/ebay.json`
= BLOCKED, `probe_not_before = 2026-08-22T18:00Z`. Probe tool: `external_engine/ebay_gentle_probe.py` (1 request,
production path, never retries; refuses before the window unless --force).

GUARD AGAINST RECURRENCE: any future edit/rsync of deep_scrub_runner.py must keep `ebay_arm_enabled` —
check with `grep -c ebay_arm_enabled mazi_db/scripts/deep_scrub_runner.py` (expect 3). The V2 engine will move
this into the SourceGovernor (`external_engine/source_health.py contact_allowed('ebay')`).
