#!/usr/bin/env python3
"""Standing scrub targets that are NOT sports players.

The subject-driven lanes (lanes.py) have only ever drawn from the sports player ledger, so anything
non-sports was covered by accident or not at all: VeeFriends rows exist in the canonical store, but
every one of them arrived because it happened to surface under some player query or a broad category
sweep. Nothing ever went looking. This module is the missing list.

Sources differ in how they are targeted, and the plan is honest about which apply:
  * eBay        — subject/query driven. These targets go straight into lanes.py freshness.
  * Fanatics    — category-shard sweeps. Picks up VeeFriends already (196 rows) with no per-subject
                  targeting available; listed here as covered-by-sweep, not as a query.
  * SCP broad   — catalog-wide but SPORTS catalogue only, so it will never carry VeeFriends.
  * Goldin/REA  — auction-house catalogues; incidental coverage only when a lot happens to include it.

Plan data lives in <store>/external_store/rosters/scrub_plan.json so it can be edited without a code
change. This module only loads, validates and flattens it.

  python3 external_engine/scrub_plan.py --list            # show the plan
  python3 external_engine/scrub_plan.py --subjects vee_friends
"""
from __future__ import annotations

import argparse
import json
import os
import sys

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
PLAN_PATH = os.path.join(STORE, "external_store", "rosters", "scrub_plan.json")

# Shipped default, written to PLAN_PATH on first use. Edit the JSON, not this.
DEFAULT_PLAN = {
    "version": 1,
    "note": "Standing non-player scrub targets. eBay queries are used verbatim by lanes.py freshness.",
    "groups": [
        {
            "key": "vee_friends",
            "label": "VeeFriends",
            "enabled": True,
            "added": "2026-09-14",
            "why": "Gary Vaynerchuk's card line. Present in canonical but never targeted; all existing "
                   "rows arrived incidentally. Non-sports, so the SCP sports catalogue cannot cover it.",
            "ebay_queries": [
                "veefriends card",
                "vee friends card",
                "topps veefriends",
                "veefriends compete and collect",
                "veefriends series 2",
                "veefriends zerocool",
                "veefriends super stickers",
                "veefriends chrome",
                "veefriends 1/1",
                "veefriends auto",
                "veefriends psa",
                "veefriends bgs",
                "veefriends very lucky black cat",
                "veefriends gift goat",
                "veefriends gary bee",
                "veefriends notorious ninja",
                "veefriends empathy elephant",
                "veefriends kind warrior",
                "veefriends observant oyster",
                "veefriends hologram wizard",
            ],
            "covered_by_sweep": ["fanatics", "goldin", "rea"],
            "not_applicable": ["sportscardspro"],
            "classifier_category": "veefriends",
        }
    ],
}


def load(path: str = PLAN_PATH) -> dict:
    """Load the plan, writing the shipped default if none exists yet."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(DEFAULT_PLAN, f, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass          # read-only store (TCC): fall back to the in-memory default
        return DEFAULT_PLAN


def subjects(group: str | None = None, path: str = PLAN_PATH) -> list[str]:
    """eBay query strings for one group, or every enabled group. Order preserved, deduped."""
    out: list[str] = []
    for g in load(path).get("groups", []):
        if not g.get("enabled", True):
            continue
        if group and g.get("key") != group:
            continue
        for q in g.get("ebay_queries", []):
            if q not in out:
                out.append(q)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--subjects", nargs="?", const=None, default="__none__",
                    help="print eBay queries (optionally for one group key)")
    a = ap.parse_args()
    plan = load()
    if a.subjects != "__none__":
        for s in subjects(a.subjects):
            print(s)
        return 0
    if a.list or True:
        print(f"scrub plan v{plan.get('version')} — {PLAN_PATH}")
        for g in plan.get("groups", []):
            state = "enabled" if g.get("enabled", True) else "DISABLED"
            print(f"\n  [{state}] {g['key']}  ({g.get('label')})   added {g.get('added')}")
            print(f"    why: {g.get('why','')}")
            print(f"    ebay queries      : {len(g.get('ebay_queries', []))}")
            print(f"    covered by sweep  : {', '.join(g.get('covered_by_sweep', [])) or '-'}")
            print(f"    not applicable to : {', '.join(g.get('not_applicable', [])) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
