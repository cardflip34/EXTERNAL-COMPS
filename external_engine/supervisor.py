#!/usr/bin/env python3
"""External Comps V2 supervisor — the "cannot silently stop" loop (PRD V2 §45/§46/§49).

Runs under an sshd-spawned context (launchd → ssh localhost → nohup) so it inherits Remote Login's Full
Disk Access to the 6 TB store; see ops/extcomps_keepalive.sh. Single instance (fcntl lock in $HOME).

Each tick (60 s):
  * resource governor sample (cheap)                          -> state file
  * heartbeat every HEARTBEAT_MIN (20) minutes                -> <store>/heartbeat/heartbeat.json (+ history)
  * eBay probe scheduler: when source_health.ebay is BLOCKED/COOLDOWN and now >= probe_not_before and no probe
    in the last 72 h -> run external_engine/ebay_gentle_probe.py ONCE (never --force); the probe updates state.
  * (lanes — freshness/backfill workers — attach here later, gated by source_health + governor)
State: <store>/heartbeat/supervisor_state.json (fallback ~/mazi_local_evidence/…), log in
~/Library/Logs/mazi_external_comps/extcomps_supervisor.out.

  nohup /usr/bin/python3 -u external_engine/supervisor.py >> ~/Library/Logs/mazi_external_comps/extcomps_supervisor.out 2>&1 &
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import resource_governor as rg  # noqa: E402
import source_health as sh  # noqa: E402

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
PY = "/usr/bin/python3"
TICK_S = 60
HEARTBEAT_MIN = 20
PROBE_MIN_GAP_H = 72
LOCK_PATH = os.path.expanduser("~/mazi_local_evidence/extcomps_supervisor.lock")
VERSION = "0.1.0"

_stop = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(msg: str) -> None:
    print(f"[{_now().isoformat(timespec='seconds')}] {msg}", flush=True)


def _state_dir() -> str:
    d = os.path.join(STORE, "heartbeat")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".w"), "w") as f:
            f.write("1")
        os.remove(os.path.join(d, ".w"))
        return d
    except OSError:
        d = os.path.expanduser("~/mazi_local_evidence/ebay_scrub_store/heartbeat")
        os.makedirs(d, exist_ok=True)
        return d


def _write_state(state: dict) -> None:
    p = os.path.join(_state_dir(), "supervisor_state.json")
    state["updated_at"] = _now().isoformat(timespec="seconds")
    with open(p + ".tmp", "w") as f:
        json.dump(state, f, indent=2)
    os.replace(p + ".tmp", p)


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:  # pragma: no cover
        return 125, f"{type(e).__name__}: {e}"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def maybe_probe(state: dict) -> None:
    """Run the single gentle eBay probe when the quiet window has elapsed (operator GO 2026-08-19)."""
    doc = sh.load("ebay", STORE)
    st = doc.get("state")
    if st not in ("BLOCKED", "COOLDOWN"):
        return
    nb = _parse_iso(doc.get("probe_not_before"))
    if nb and _now() < nb:
        state["next_probe_at"] = nb.isoformat(timespec="seconds")
        return
    last = _parse_iso((doc.get("last_probe") or {}).get("at"))
    if last and _now() - last < timedelta(hours=PROBE_MIN_GAP_H):
        state["next_probe_at"] = (last + timedelta(hours=PROBE_MIN_GAP_H)).isoformat(timespec="seconds")
        return
    level, _ = rg.classify(rg.sample())
    if level == "RED":
        _log("probe deferred: resource RED")
        return
    _log("running eBay gentle probe (1 request, production path)")
    rc, out = _run([PY, os.path.join(HERE, "ebay_gentle_probe.py")], timeout=180)
    state["last_probe_run"] = {"at": _now().isoformat(timespec="seconds"), "rc": rc, "tail": out[-600:]}
    _log(f"probe finished rc={rc}: {out.strip().splitlines()[-1] if out.strip() else ''}")
    if rc == 0:
        sh.add_event("ebay", "operator_attention", detail="GREEN probe — eBay resumption pending coordination (polite mode, low volume)", store=STORE)


def heartbeat(state: dict) -> None:
    rc, out = _run([PY, os.path.join(HERE, "heartbeat.py")], timeout=120)
    state["last_heartbeat"] = {"at": _now().isoformat(timespec="seconds"), "rc": rc,
                               "verdict": {0: "OK", 1: "WARN", 2: "CRIT"}.get(rc, f"rc{rc}"),
                               "summary": out.strip().splitlines()[0] if out.strip() else ""}
    _log(f"heartbeat {state['last_heartbeat']['verdict']}: {state['last_heartbeat']['summary']}")


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    _log(f"signal {signum} received; stopping after this tick")


def main() -> int:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock = open(LOCK_PATH, "a+")  # never truncate before we own the lock (keep-alive reads the PID from it)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another supervisor instance holds the lock; exiting", flush=True)
        return 0
    lock.seek(0); lock.truncate(); lock.write(str(os.getpid())); lock.flush()
    signal.signal(signal.SIGTERM, _on_signal); signal.signal(signal.SIGINT, _on_signal)
    state = {"version": VERSION, "pid": os.getpid(), "started_at": _now().isoformat(timespec="seconds"),
             "host": os.uname().nodename, "store": STORE, "store_writable": _state_dir().startswith(STORE),
             "ticks": 0}
    _log(f"supervisor v{VERSION} start pid={os.getpid()} store_writable={state['store_writable']}")
    last_hb = None
    while not _stop:
        t0 = time.time()
        try:
            res = rg.sample(); level, reasons = rg.classify(res)
            state["resource"] = {"level": level, "reasons": reasons, "at": res["at"]}
            if last_hb is None or time.time() - last_hb >= HEARTBEAT_MIN * 60:
                heartbeat(state); last_hb = time.time()
            maybe_probe(state)
            state["ticks"] += 1
            _write_state(state)
        except Exception as e:  # never die on a tick error
            _log(f"tick error: {type(e).__name__}: {e}")
        time.sleep(max(1.0, TICK_S - (time.time() - t0)))
    state["stopped_at"] = _now().isoformat(timespec="seconds"); _write_state(state)
    _log("supervisor stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
