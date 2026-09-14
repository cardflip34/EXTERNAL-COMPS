#!/usr/bin/env bash
# Copy lane artifacts from the Mini working project into this repo (source of truth = ~/whatnot-sniper).
set -euo pipefail
P="$HOME/whatnot-sniper"; R="$(cd "$(dirname "$0")" && pwd)"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$P/external_engine/" "$R/external_engine/"
rsync -a "$P/docs/EXTERNAL_"*.md "$R/docs/"
rsync -a "$P/ops/mini/phase_scrub_top1000_continuous.sh" "$P/ops/mini/nightly_delta_merge.py" "$R/ops/mini/"
rsync -a "$P/ops/DEEP_SCRUB_EBAY_GATE_NOTE.md" "$P/ops/extcomps_keepalive.sh" "$R/ops/"; mkdir -p "$R/ops/launchd"; cp "$HOME/Library/LaunchAgents/com.mazi.extcomps-keepalive.plist" "$R/ops/launchd/" 2>/dev/null || true
# Every ops script the supervisor actually launches. This list was hand-maintained and had silently
# fallen behind: canonical_refresh.sh and image_backfill_healer.sh -- both named in supervisor.HEALERS
# -- existed only on the Mini and were in no repo at all. Rebuild the box and the supervisor would
# come up referencing scripts that no longer exist. Sync from the healer list, not from memory.
for f in canonical_refresh.sh image_backfill_healer.sh; do
  [ -f "$P/ops/$f" ] && rsync -a "$P/ops/$f" "$R/ops/"
done
# tripwire: anything HEALERS points at under ops/ must have landed above
/usr/bin/python3 - "$P" "$R" <<'PY'
import os, re, sys
proj, repo = sys.argv[1], sys.argv[2]
src = open(os.path.join(proj, "external_engine", "supervisor.py")).read()
missing = [m for m in re.findall(r'ops/([A-Za-z0-9_./-]+\.(?:sh|py))', src)
           if os.path.exists(os.path.join(proj, "ops", m)) and not os.path.exists(os.path.join(repo, "ops", m))]
if missing:
    sys.exit("SYNC GAP: supervisor references ops/ scripts this repo does not carry: " + ", ".join(missing))
PY
# aggregate-only reports (no PII): store audit md/json
S="/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store"
[ -f "$S/audit/store_audit_latest.md" ] && cp -L "$S/audit/store_audit_latest.md" "$R/reports/store_audit_latest.md"
[ -f "$S/source_health/ebay.json" ] && mkdir -p "$R/reports/source_health" && cp "$S/source_health/ebay.json" "$R/reports/source_health/ebay.json"
echo "synced $(date -u +%FT%TZ)"
