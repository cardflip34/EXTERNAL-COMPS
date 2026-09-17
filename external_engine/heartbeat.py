#!/usr/bin/env python3
"""Heartbeat (PRD V2 §45) — one JSON snapshot every run: who is alive, source states, resource level,
freshest canonical write, launchd "loaded-but-dead" detection. Designed to be run every 20 min by the
V2 supervisor (or by hand). Writes to <store>/heartbeat/heartbeat.json (falls back to ~/mazi_local_evidence)
and appends a one-line JSONL history. Exit code: 0 OK, 1 WARN, 2 CRIT.

  python3 external_engine/heartbeat.py [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import resource_governor as rg  # noqa: E402
import source_health as sh  # noqa: E402

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
LAUNCHD_LABELS = ["com.mazi.deep-scrub-nightly", "com.mazi.deep-scrub-drain"]
EXPECTED_PROCS = {  # name -> pgrep pattern; absence is informational until the V2 services exist
    "phase_backfill": "run_player_scrub_phase.py --phase|ebay_player_scraper.py --player",
    "sources_supervisor": "tools/sources_supervisor.py",
    "deep_scrub_runner": "deep_scrub_runner.py",
    "canonical_migrate": "canonical_migrate.py",
}
FRESHNESS_WARN_H, FRESHNESS_CRIT_H = 6, 24


def _pgrep(pattern: str) -> list[int]:
    out = subprocess.run(["/usr/bin/pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    return [int(p) for p in out if p.isdigit()]


def _launchd(label: str) -> dict:
    out = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    if out.returncode != 0:
        return {"loaded": False}
    d = {"loaded": True}
    for line in out.stdout.splitlines():
        s = line.strip()
        for key in ("state", "runs", "last exit code"):
            if s.startswith(key + " ="):
                d[key.replace(" ", "_")] = s.split("=", 1)[1].strip()
    return d


def _newest_mtime(paths: list[str]) -> float | None:
    best = None
    for p in paths:
        if os.path.exists(p):
            m = os.path.getmtime(p)
            best = m if best is None or m > best else best
    return best


def _hours_since(ts: float | None) -> float | None:
    return None if ts is None else round((time.time() - ts) / 3600, 2)


def build() -> dict:
    res = rg.sample(); level, reasons = rg.classify(res)
    ebay = sh.load("ebay", STORE)
    # Every source that has a health file, not just eBay. This reported ebay alone, so when SCP -- the
    # eBay substitute carrying ~10M rows -- fell to ~12% success for days, there was nowhere for that
    # to show up: the watchdog said "driver alive, bridge alive" and the row count still crept up.
    tracked = {}
    try:
        for fn in sorted(os.listdir(os.path.join(STORE, "source_health"))):
            if fn.endswith(".json"):
                tracked[fn[:-5]] = sh.load(fn[:-5], STORE)
    except OSError:
        tracked = {"ebay": ebay}
    store_files = [os.path.join(STORE, f) for f in ("player_comps.json", "raw_player_comps.json", "ebay_comps.json")]
    canon_dir = os.path.join(STORE, "external_store", "parquet")
    canon_newest = None
    for root, _, fs in os.walk(canon_dir):
        for f in fs:
            if f.endswith(".parquet"):
                m = os.path.getmtime(os.path.join(root, f))
                canon_newest = m if canon_newest is None or m > canon_newest else canon_newest
    # freshness-lag artifact (written by the 6-hourly canonical refresh via freshness_report.py;
    # heartbeat only READS it so the 20-min loop stays cheap)
    fresh = {}
    fresh_path = os.path.join(STORE, "external_store", "freshness", "freshness_latest.json")
    try:
        with open(fresh_path) as f:
            fresh = json.load(f)
        fresh["artifact_age_h"] = round((time.time() - os.path.getmtime(fresh_path)) / 3600, 1)
    except (OSError, ValueError):
        fresh = {"error": "freshness artifact missing/unreadable"}
    hb = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": os.uname().nodename,
        "resource": {"level": level, "reasons": reasons, **{k: res[k] for k in ("ram_reclaimable_gb", "swap_used_gb", "load1", "disk_store_free_gb", "browser_procs", "store_mounted")}},
        "sources": {name: {"state": d.get("state"), "since": d.get("state_since"),
                           "probe_not_before": d.get("probe_not_before"),
                           "last_probe": (d.get("last_probe") or {}).get("verdict")}
                    for name, d in tracked.items()},
        "processes": {name: _pgrep(pat) for name, pat in EXPECTED_PROCS.items()},
        "launchd": {lbl: _launchd(lbl) for lbl in LAUNCHD_LABELS},
        "freshness": {"per_source": {k: f"{v.get('lag_days')}d/{v.get('status')}" for k, v in (fresh.get("sources") or {}).items()},
                      "alerts": fresh.get("alerts", []), "artifact_age_h": fresh.get("artifact_age_h"),
                      "error": fresh.get("error")},
        "store": {"legacy_json_newest_write_h_ago": _hours_since(_newest_mtime(store_files)),
                  "canonical_parquet_newest_write_h_ago": _hours_since(canon_newest)},
    }
    # verdict
    crit, warn = [], []
    if level == "RED":
        crit.append("resource RED")
    elif level == "YELLOW":
        warn.append("resource YELLOW")
    if not res["store_mounted"]:
        crit.append("store volume missing")
    for name, d in tracked.items():
        st = d.get("state")
        if st in ("BLOCKED", "AUTH_REQUIRED", "CONFIG_ERROR", "OFFLINE", "DEGRADED"):
            note = (d.get("note") or "").split(".")[0]
            warn.append(f"{name} {st}" + (f" — {note}" if note else ""))
    fresh_h = hb["store"]["canonical_parquet_newest_write_h_ago"]
    legacy_h = hb["store"]["legacy_json_newest_write_h_ago"]
    newest_h = min(x for x in (fresh_h, legacy_h) if x is not None) if (fresh_h is not None or legacy_h is not None) else None
    if newest_h is not None:
        if newest_h > FRESHNESS_CRIT_H:
            crit.append(f"no store write for {newest_h:.0f} h (freshness CRIT > {FRESHNESS_CRIT_H} h)")
        elif newest_h > FRESHNESS_WARN_H:
            warn.append(f"no store write for {newest_h:.0f} h")
    for al in (fresh.get("alerts") or []):
        warn.append(f"freshness: {al}")
    if fresh.get("error"):
        warn.append(f"freshness artifact: {fresh['error']}")
    elif (fresh.get("artifact_age_h") or 0) > 8:
        warn.append(f"freshness artifact stale ({fresh['artifact_age_h']} h old — canonical refresh may be failing)")
    for lbl, d in hb["launchd"].items():
        lec = d.get("last_exit_code") or ""
        if d.get("loaded") and lec not in (None, "", "0") and "never exited" not in lec:
            warn.append(f"{lbl} last exit {d.get('last_exit_code')}")
    hb["verdict"] = "CRIT" if crit else ("WARN" if warn else "OK")
    hb["crit"] = crit; hb["warn"] = warn
    return hb


def write(hb: dict) -> str:
    base = os.path.join(STORE, "heartbeat")
    try:
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, ".w"), "w") as f:
            f.write("1")
        os.remove(os.path.join(base, ".w"))
    except OSError:
        base = os.path.expanduser("~/mazi_local_evidence/ebay_scrub_store/heartbeat")
        os.makedirs(base, exist_ok=True)
    p = os.path.join(base, "heartbeat.json")
    with open(p + ".tmp", "w") as f:
        json.dump(hb, f, indent=2)
    os.replace(p + ".tmp", p)
    with open(os.path.join(base, "heartbeat_history.jsonl"), "a") as f:
        f.write(json.dumps({"at": hb["at"], "verdict": hb["verdict"], "resource": hb["resource"]["level"],
                            "ebay": hb["sources"]["ebay"]["state"], "crit": hb["crit"], "warn": hb["warn"]}) + "\n")
    return p


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--json", action="store_true"); a = ap.parse_args()
    hb = build(); p = write(hb)
    if a.json:
        print(json.dumps(hb, indent=2))
    else:
        print(f"{hb['verdict']}  resource={hb['resource']['level']}  ebay={hb['sources']['ebay']['state']}  → {p}")
        for c in hb["crit"]: print("  CRIT:", c)
        for w in hb["warn"]: print("  WARN:", w)
    sys.exit({"OK": 0, "WARN": 1, "CRIT": 2}[hb["verdict"]])


if __name__ == "__main__":
    main()
