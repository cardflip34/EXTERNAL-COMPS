#!/usr/bin/env python3
"""
resort_targets.py — reorder the UNSWEPT SCP targets by release year, not sales volume.

WHY: targets.jsonl was built strictly volume-descending. Volume turns out to be the
wrong predictor of comp yield. Measured over 259,439 already-swept cards:

    release <2015 -> 97.5% hit, 42.2 rows/card, 94.5% reach pre-2024
    release 2025  -> 29.8% hit, 11.0 rows/card,  0.0% reach pre-2024

The cause is structural: an SCP card page holds only ~30 sales per grade bucket, so a
hot modern card's entire ledger is recent and can never reach back, while a slow
vintage card's 30 sales span years. Yet 2024+ releases are 458,225 of the 740,240
unswept cards (62%) — the engine was about to spend most of its remaining budget on
the cards that yield least and cannot deepen 2023/2024 at all.

Reordering does NOT change the lifetime total; it changes DELIVERY ORDER, front-loading
the deep-history cards (est. ~1.6M pre-2024 rows arriving in ~26 days instead of ~106).

SAFETY: the swept prefix keeps its original order and is left untouched (the engine
skips those by id anyway — `done_ids` is card-keyed, so the resume cursor stays valid
regardless of file order). Backup written before any change; atomic replace.
"""
import json
import os
import re
import shutil
import sys
from collections import Counter

D = os.path.expanduser("~/mazi_scp_broad")
TARGETS = os.path.join(D, "targets.jsonl")
STATE = os.path.join(D, "scp_broad_state.json")
BACKUP = os.path.join(D, "targets.jsonl.bak.volumeorder_20260731")

YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")


def release_year(console: str) -> int:
    """First 4-digit year in the console name ('Baseball Cards 1989 Upper Deck' -> 1989).
    Cards with no parseable year sort LAST (unknown, not assumed-old)."""
    m = YEAR_RE.search(console or "")
    return int(m.group(1)) if m else 9999


def main() -> int:
    dry = "--commit" not in sys.argv
    done = set(json.load(open(STATE))["done_ids"])
    swept, unswept = [], []
    for line in open(TARGETS):
        line = line.rstrip("\n")
        if not line:
            continue
        d = json.loads(line)
        (swept if d["id"] in done else unswept).append((d, line))

    # release year ASC, then volume DESC within a year
    unswept.sort(key=lambda t: (release_year(t[0].get("console", "")),
                                -int(t[0].get("volume", 0) or 0)))

    print(f"swept (unchanged): {len(swept):,}   unswept (reordered): {len(unswept):,}")
    head = Counter()
    for d, _ in unswept[:50000]:
        y = release_year(d.get("console", ""))
        head["<2015" if y < 2015 else ("2015-2019" if y < 2020 else
             ("2020-2023" if y < 2024 else ("2024+" if y < 9999 else "no-year")))] += 1
    print("first 50,000 after reorder:", dict(head))
    print("first 3 targets now:")
    for d, _ in unswept[:3]:
        print(f"   {release_year(d.get('console','')):>4}  vol={d.get('volume'):>6}  {d.get('console','')[:44]}")

    if dry:
        print("\n[dry-run] nothing written — pass --commit to apply")
        return 0

    shutil.copy2(TARGETS, BACKUP)
    tmp = TARGETS + ".tmp"
    with open(tmp, "w") as fh:
        for _, line in swept:
            fh.write(line + "\n")
        for _, line in unswept:
            fh.write(line + "\n")
    os.replace(tmp, TARGETS)
    n = sum(1 for _ in open(TARGETS))
    print(f"\n[done] rewrote {TARGETS} ({n:,} lines; backup {BACKUP})")
    assert n == len(swept) + len(unswept), "line count changed!"
    return 0


if __name__ == "__main__":
    sys.exit(main())
