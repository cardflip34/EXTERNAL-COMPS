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
for f in canonical_refresh.sh image_backfill_healer.sh whatnot_link_healer.sh; do
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
# 2026-09-29 catch-up: everything the lane RUNS, not just external_engine/. Paths mirror ~/whatnot-sniper.
# (Found 09-29: VeeFriends linker/catalog/beta tools, the signed-in eBay sweep, the SCP broad bridge + scrub and the
#  Fanatics high-value sweep existed only on the Mini.)
mkdir -p "$R/tools" "$R/mazi_db/scripts" "$R/ops/scp" "$R/ops/veefriends" "$R/ops/ebay_session" "$R/ops/launchd"
for f in veefriends_link.py veefriends_stickers.py veefriends_variants.py test_veefriends_link.py export_veefriends_beta.py \
         correct_veefriends_beta.py ebay_veefriends_pilot.py test_ebay_veefriends_pilot.py import_ebay_veefriends_pilot.py \
         render_pages_cdp.py fanatics_title_backfill.py ebay_signed_in_sweep.py import_ebay_sweep.py build_scp_ebay_ids.py \
         bridge_scp_broad_to_neon.py test_bridge_batch.py scp_broad_scrub.py sources_supervisor.py; do
  rsync -a "$P/tools/$f" "$R/tools/"
done
for f in fanatics_recent_refresh.py fanatics_full_catalog_scraper_v3.py fanatics_full_catalog_scraper_v3_price_bands.py \
         fanatics_extra_title_shards.json fanatics_extra_categories.json fanatics_extra_price_shards.json goldin_scraper_v2.py \
         auctionreport_scraper.py tcgplayer_sold_scraper.py myslabs_scraper_v2.py rea_scraper.py bridge_local_sources_to_neon.py; do
  rsync -a "$P/$f" "$R/"
done
rsync -a "$P/mazi_db/scripts/import_fanatics_v3_chunks_to_neon.py" "$R/mazi_db/scripts/"
rsync -a --include '*.sh' --include '*.py' --exclude '*' "$HOME/mazi_scp_broad/" "$R/ops/scp/"
rsync -a "$HOME/mazi_veefriends/daily_refresh.sh" "$HOME/mazi_veefriends/extract_checklist.py" "$R/ops/veefriends/"
rsync -a --exclude 'chrome_profile' "$HOME/mazi_ebay_session/" "$R/ops/ebay_session/"
cp "$HOME/Library/LaunchAgents/com.mazi.veefriends-daily.plist" "$R/ops/launchd/"
# tripwire: every script the sources supervisor launches must be in this repo
/usr/bin/python3 - "$P" "$R" <<'PY2'
import os, re, sys
proj, repo = sys.argv[1], sys.argv[2]
src = open(os.path.join(proj, "tools", "sources_supervisor.py")).read()
legs = set(re.findall(r'"([A-Za-z0-9_./-]+\.(?:py|sh))"', src))
missing = [f for f in legs if os.path.exists(os.path.join(proj, f)) and not os.path.exists(os.path.join(repo, f))]
if missing:
    sys.exit("SYNC GAP: sources_supervisor launches scripts this repo does not carry: " + ", ".join(sorted(missing)))
PY2
# aggregate-only reports (no PII): store audit md/json
S="/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store"
[ -f "$S/audit/store_audit_latest.md" ] && cp -L "$S/audit/store_audit_latest.md" "$R/reports/store_audit_latest.md"
[ -f "$S/source_health/ebay.json" ] && mkdir -p "$R/reports/source_health" && cp "$S/source_health/ebay.json" "$R/reports/source_health/ebay.json"
echo "synced $(date -u +%FT%TZ)"
