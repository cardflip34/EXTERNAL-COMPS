#!/usr/bin/env python3
"""Incremental line counter for the 11 GB SCP broad JSONL.

The watchdog needs a row count every 300 s for its heartbeat. `wc -l` reads the whole 11 GB
off a spinning disk each pass — under contention that took 15-43+ min, so passes stacked and
each stacked copy added another 11 GB read to the same spindle (2026-09-13 incident).

This counts only the bytes appended since the last pass and carries the running total in a
small state file next to it. Cost per pass is proportional to NEW data, not file size.

  rowcount.py <jsonl> <state.json>          incremental (requires existing state)
  rowcount.py <jsonl> <state.json> --seed   full read, establishes the baseline (slow, once)

Prints the line count, or -1 if no baseline exists yet (caller decides what to display).
"""
import json
import os
import sys

CHUNK = 8 << 20


def main() -> int:
    if len(sys.argv) < 3:
        print(-1)
        return 0
    path, state_path = sys.argv[1], sys.argv[2]
    seed = "--seed" in sys.argv[3:]
    try:
        size = os.path.getsize(path)
    except OSError:
        print(-1)
        return 0

    state = None
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        pass

    if state is None:
        if not seed:
            print(-1)  # never do a full read from the watchdog's hot path
            return 0
        offset, lines = 0, 0
    else:
        offset, lines = state.get("offset", 0), state.get("lines", 0)
        if offset > size:  # truncated or rotated -> rebuild the baseline
            offset, lines = 0, 0

    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            lines += chunk.count(b"\n")

    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"offset": size, "lines": lines}, f)
    os.replace(tmp, state_path)
    print(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
