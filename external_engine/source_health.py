#!/usr/bin/env python3
"""Source health state (PRD V2 §29 state machine) — tiny, file-backed, no deps.

States: HEALTHY | DEGRADED | COOLDOWN | BLOCKED | AUTH_REQUIRED | CONFIG_ERROR | OFFLINE

File: <store>/source_health/<source>.json  (store root defaults to MAZI_EBAY_SCRUB_STORE or the 6TB path;
falls back to ~/mazi_local_evidence/ebay_scrub_store when the volume is not writable — same fallback
the deep-scrub wrappers use for TCC-restricted launchd contexts).

CLI:
  python3 external_engine/source_health.py show ebay
  python3 external_engine/source_health.py set ebay BLOCKED --note "..." [--probe-not-before ISO]
  python3 external_engine/source_health.py event ebay block_confirmed --detail "..."
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

STATES = ("HEALTHY", "DEGRADED", "COOLDOWN", "BLOCKED", "AUTH_REQUIRED", "CONFIG_ERROR", "OFFLINE")
DEFAULT_STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
HOME_FALLBACK = os.path.expanduser("~/mazi_local_evidence/ebay_scrub_store")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def health_dir(store: str | None = None) -> str:
    base = store or DEFAULT_STORE
    d = os.path.join(base, "source_health")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".w")
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return d
    except OSError:
        d = os.path.join(HOME_FALLBACK, "source_health")
        os.makedirs(d, exist_ok=True)
        return d


def path_for(source: str, store: str | None = None) -> str:
    return os.path.join(health_dir(store), f"{source}.json")


def load(source: str, store: str | None = None) -> dict:
    p = path_for(source, store)
    if not os.path.exists(p):
        return {"source": source, "state": "HEALTHY", "events": [], "updated_at": None}
    with open(p) as f:
        return json.load(f)


def save(doc: dict, store: str | None = None) -> str:
    doc["updated_at"] = _now()
    p = path_for(doc["source"], store)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, p)
    return p


def set_state(source: str, state: str, note: str = "", store: str | None = None, **fields) -> dict:
    state = state.upper()
    if state not in STATES:
        raise ValueError(f"bad state {state}; allowed {STATES}")
    doc = load(source, store)
    prev = doc.get("state")
    doc["state"] = state
    doc["state_since"] = _now() if prev != state else doc.get("state_since") or _now()
    for k, v in fields.items():
        if v is not None:
            doc[k] = v
    doc.setdefault("events", []).append({"at": _now(), "type": "state_change", "from": prev, "to": state, "note": note})
    doc["events"] = doc["events"][-200:]
    save(doc, store)
    return doc


def add_event(source: str, etype: str, detail: str = "", store: str | None = None, **fields) -> dict:
    doc = load(source, store)
    ev = {"at": _now(), "type": etype, "detail": detail}
    ev.update({k: v for k, v in fields.items() if v is not None})
    doc.setdefault("events", []).append(ev)
    doc["events"] = doc["events"][-200:]
    save(doc, store)
    return doc


def contact_allowed(source: str, store: str | None = None) -> tuple[bool, str]:
    """Governor hook: may an adapter contact this source right now?"""
    doc = load(source, store)
    st = doc.get("state", "HEALTHY")
    if st in ("BLOCKED", "COOLDOWN", "AUTH_REQUIRED", "CONFIG_ERROR", "OFFLINE"):
        return False, f"{source} state={st} since {doc.get('state_since')}; {doc.get('state_note','')}"
    return True, f"{source} state={st}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show"); s.add_argument("source")
    t = sub.add_parser("set"); t.add_argument("source"); t.add_argument("state"); t.add_argument("--note", default="")
    t.add_argument("--probe-not-before", default=None); t.add_argument("--since", default=None)
    e = sub.add_parser("event"); e.add_argument("source"); e.add_argument("etype"); e.add_argument("--detail", default="")
    c = sub.add_parser("allowed"); c.add_argument("source")
    for p in (s, t, e, c):
        p.add_argument("--store", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "show":
        print(json.dumps(load(a.source, a.store), indent=2))
    elif a.cmd == "set":
        doc = set_state(a.source, a.state, a.note, a.store, probe_not_before=a.probe_not_before,
                        block_observed_since=a.since, state_note=a.note or None)
        print(json.dumps({k: v for k, v in doc.items() if k != "events"}, indent=2))
    elif a.cmd == "event":
        add_event(a.source, a.etype, a.detail, a.store)
        print("ok")
    elif a.cmd == "allowed":
        ok, why = contact_allowed(a.source, a.store)
        print(("ALLOWED " if ok else "DENIED ") + why)
        sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
