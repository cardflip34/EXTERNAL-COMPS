#!/usr/bin/env bash
# Copy lane artifacts from the Mini working project into this repo (source of truth = ~/whatnot-sniper).
set -euo pipefail
P="$HOME/whatnot-sniper"; R="$(cd "$(dirname "$0")" && pwd)"
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' "$P/external_engine/" "$R/external_engine/"
rsync -a "$P/docs/EXTERNAL_"*.md "$R/docs/"
rsync -a "$P/ops/mini/phase_scrub_top1000_continuous.sh" "$P/ops/mini/nightly_delta_merge.py" "$R/ops/mini/"
rsync -a "$P/ops/DEEP_SCRUB_EBAY_GATE_NOTE.md" "$P/ops/extcomps_keepalive.sh" "$R/ops/"; mkdir -p "$R/ops/launchd"; cp "$HOME/Library/LaunchAgents/com.mazi.extcomps-keepalive.plist" "$R/ops/launchd/" 2>/dev/null || true
# aggregate-only reports (no PII): store audit md/json
S="/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store"
[ -f "$S/audit/store_audit_latest.md" ] && cp -L "$S/audit/store_audit_latest.md" "$R/reports/store_audit_latest.md"
[ -f "$S/source_health/ebay.json" ] && mkdir -p "$R/reports/source_health" && cp "$S/source_health/ebay.json" "$R/reports/source_health/ebay.json"
echo "synced $(date -u +%FT%TZ)"
