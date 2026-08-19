#!/usr/bin/env python3
"""Resource governor (PRD V2 §42) — GREEN / YELLOW / RED from RAM, swap, load, disk, browser count.

No third-party deps (psutil optional): uses vm_stat / sysctl / os.getloadavg / statvfs so it runs under
/usr/bin/python3 from launchd or ssh. Thresholds are tuned for the 16 GB Mini sharing RAM with the capture fleet.

  GREEN  : normal plan
  YELLOW : Lane B (historical) paused, Lane A (freshness) 1 worker
  RED    : all lanes paused, alert

CLI:  python3 external_engine/resource_governor.py [--json]      (exit 0 GREEN, 1 YELLOW, 2 RED)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

STORE_VOLUME = "/Volumes/MAZI_EVIDENCE_6TB"
THRESHOLDS = {
    # free RAM GB (inactive+free+speculative counted as reclaimable), swap used GB, 1-min load, disk free GB, browsers
    "yellow": {"free_gb_lt": 2.0, "swap_used_gb_ge": 1.0, "load1_ge": 12.0, "disk_free_gb_lt": 100.0, "browsers_ge": 6},
    "red":    {"free_gb_lt": 0.75, "swap_used_gb_ge": 1.8, "load1_ge": 24.0, "disk_free_gb_lt": 25.0, "browsers_ge": 12},
}


def _vm_stat() -> dict:
    out = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True).stdout
    page = 16384
    m = re.search(r"page size of (\d+) bytes", out)
    if m:
        page = int(m.group(1))
    vals = {}
    for line in out.splitlines():
        mm = re.match(r"\s*(.+?):\s+(\d+)\.", line)
        if mm:
            vals[mm.group(1).strip().lower()] = int(mm.group(2)) * page / 1e9
    return {
        "free_gb": round(vals.get("pages free", 0.0), 3),
        "inactive_gb": round(vals.get("pages inactive", 0.0), 3),
        "speculative_gb": round(vals.get("pages speculative", 0.0), 3),
        "compressed_gb": round(vals.get("pages occupied by compressor", 0.0), 3),
        "active_gb": round(vals.get("pages active", 0.0), 3),
        "wired_gb": round(vals.get("pages wired down", 0.0), 3),
    }


def _swap_used_gb() -> float:
    out = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    m = re.search(r"used = ([\d.]+)M", out)
    return round(float(m.group(1)) / 1024, 3) if m else 0.0


def _total_ram_gb() -> float:
    out = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()
    return round(int(out) / 1e9, 1) if out.isdigit() else 0.0


def _browser_count() -> int:
    out = subprocess.run(["/bin/ps", "-axo", "command"], capture_output=True, text=True).stdout
    n = 0
    for line in out.splitlines():
        l = line.lower()
        if ("chromium" in l or "chrome" in l or "ms-playwright" in l) and ("--type=renderer" in l or "headless" in l or "ms-playwright" in l):
            n += 1
    return n


def _disk_free_gb(path: str) -> float | None:
    try:
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / 1e9, 1)
    except OSError:
        return None


def sample() -> dict:
    vm = _vm_stat()
    reclaimable = vm["free_gb"] + vm["inactive_gb"] + vm["speculative_gb"]
    s = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": os.uname().nodename,
        "ram_total_gb": _total_ram_gb(),
        "ram_free_gb": vm["free_gb"],
        "ram_reclaimable_gb": round(reclaimable, 3),
        "ram_compressed_gb": vm["compressed_gb"],
        "swap_used_gb": _swap_used_gb(),
        "load1": round(os.getloadavg()[0], 2),
        "load5": round(os.getloadavg()[1], 2),
        "disk_root_free_gb": _disk_free_gb("/"),
        "disk_store_free_gb": _disk_free_gb(STORE_VOLUME),
        "store_mounted": os.path.ismount(STORE_VOLUME),
        "browser_procs": _browser_count(),
    }
    return s


def classify(s: dict) -> tuple[str, list[str]]:
    reasons = []
    # use reclaimable (free+inactive) as the RAM signal; macOS keeps "free" tiny by design
    ram = s["ram_reclaimable_gb"]
    disk = s["disk_store_free_gb"] if s["disk_store_free_gb"] is not None else 0.0
    level = "GREEN"
    r, y = THRESHOLDS["red"], THRESHOLDS["yellow"]
    if not s["store_mounted"]:
        return "RED", ["store volume not mounted"]
    checks = [
        ("ram_reclaimable_gb", ram < r["free_gb_lt"], ram < y["free_gb_lt"], f"reclaimable RAM {ram:.2f} GB"),
        ("swap_used_gb", s["swap_used_gb"] >= r["swap_used_gb_ge"], s["swap_used_gb"] >= y["swap_used_gb_ge"], f"swap used {s['swap_used_gb']:.2f} GB"),
        ("load1", s["load1"] >= r["load1_ge"], s["load1"] >= y["load1_ge"], f"load1 {s['load1']}"),
        ("disk_store_free_gb", disk < r["disk_free_gb_lt"], disk < y["disk_free_gb_lt"], f"store disk free {disk} GB"),
        ("browser_procs", s["browser_procs"] >= r["browsers_ge"], s["browser_procs"] >= y["browsers_ge"], f"browser procs {s['browser_procs']}"),
    ]
    for _, is_red, is_yellow, label in checks:
        if is_red:
            level = "RED"; reasons.append(f"RED: {label}")
        elif is_yellow:
            if level != "RED":
                level = "YELLOW"
            reasons.append(f"YELLOW: {label}")
    return level, reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    s = sample()
    level, reasons = classify(s)
    s["level"] = level; s["reasons"] = reasons
    if a.json:
        print(json.dumps(s, indent=2))
    else:
        print(f"{level}  " + "; ".join(reasons) if reasons else f"{level}  (all clear)")
        print(f"  RAM reclaimable {s['ram_reclaimable_gb']} GB (free {s['ram_free_gb']}), swap {s['swap_used_gb']} GB, "
              f"load1 {s['load1']}, store free {s['disk_store_free_gb']} GB, browsers {s['browser_procs']}")
    sys.exit({"GREEN": 0, "YELLOW": 1, "RED": 2}[level])


if __name__ == "__main__":
    main()
