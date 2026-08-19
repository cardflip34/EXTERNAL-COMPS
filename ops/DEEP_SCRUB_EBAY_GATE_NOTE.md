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
