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
VERSION = "0.5.0"
LOGDIR = os.path.expanduser("~/Library/Logs/mazi_external_comps")

# Managed lanes (PRD §46/§49): long-running children the supervisor keeps alive, gated by the resource governor.
# Each inherits this process's sshd context (FDA to the store). NO eBay anywhere in these lanes — eBay is gated
# separately by source_health until the probe is GREEN and the polite adapter ships.
MANAGED_LANES = [
    {
        "name": "sources_supervisor",           # Fanatics / Goldin / TCGplayer / MySlabs / REA / AuctionReport → Neon
        "cmd": [PY, "-u", "tools/sources_supervisor.py"],
        "pattern": "tools/sources_supervisor.py",
        "log": os.path.join(LOGDIR, "sources_supervisor.out"),
        "max_level": "YELLOW",                  # run at GREEN/YELLOW; stop after RED_STOP_TICKS consecutive RED ticks
        "enabled": os.environ.get("EXTCOMPS_LANE_SOURCES", "1") == "1",
    },
]
RED_STOP_TICKS = 3
LEVEL_RANK = {"GREEN": 0, "YELLOW": 1, "RED": 2}

# Healers (PRD §46): idempotent "check-and-relaunch" scripts run every N ticks. Unlike lanes (one long-running
# child the supervisor owns), a healer is a legacy watchdog that manages its OWN children and exits. Running it
# here makes it reboot-safe via the keep-alive→ssh→supervisor chain (the SCP broad watchdog died on the Aug-15
# reboot because its launchd agent wasn't loaded — this prevents a repeat without depending on launchd bootstrap).
HEALERS = [
    {
        "name": "canonical_refresh",  # keeps external_market_canonical_v1 tracking the live scrubs
        # Added 2026-08-25: canonicalization was manual and had drifted 4 days / ~2.4M rows behind the
        # scrapers (scrapers healthy, store stale — a silent freshness failure). The script is idempotent
        # and self-rate-limited (>=6h between rebuilds), so running it often is harmless.
        "cmd": ["/bin/bash", os.path.join(ROOT, "ops/canonical_refresh.sh")],
        "every_ticks": 60,              # check hourly; the script itself enforces the 6h floor
        "max_level": "YELLOW",          # never rebuild under RED — it is a heavy DuckDB job
        # DETACHED: this job runs ~40+ min. Waiting on it inside the tick would block the supervisor,
        # and the old 120 s _run() timeout SIGKILLed bash mid-run — the orphaned python finished the
        # ingest, but the classifier, the success stamp and the lock cleanup never ran, so it silently
        # re-fired every hour (observed 2026-08-31). Fire-and-forget is correct: the script owns its
        # own single-instance lock and 6 h rate limit.
        "detach": True,
        "enabled": os.environ.get("EXTCOMPS_HEALER_CANONICAL", "1") == "1"
                   and os.path.exists(os.path.join(ROOT, "ops/canonical_refresh.sh")),
    },
    {
        "name": "scp_broad_watchdog",   # revives run_scp_broad.sh (Jina→SCP, self-throttling) + its Neon bridge
        "cmd": ["/bin/bash", os.path.expanduser("~/mazi_scp_broad/scp_broad_watchdog.sh")],
        "every_ticks": 5,               # ~5 min at TICK_S=60, matching the original launchd StartInterval
        "max_level": "RED",
        # ALWAYS RUN (2026-09-04): this is the SAFETY net (kills a wedged scrubber, revives a dead bridge),
        # not a workload — it costs milliseconds. Gating it on resource level meant a false RED skipped it
        # 179 consecutive times, i.e. the stall detector was disabled exactly when the box looked unhealthy.
        # A watchdog that switches itself off under load is worse than no watchdog.
        "always_run": True,
        "enabled": os.environ.get("EXTCOMPS_HEALER_SCP_BROAD", "1") == "1" and os.path.exists(os.path.expanduser("~/mazi_scp_broad/scp_broad_watchdog.sh")),
    },
]

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


def _lane_pids(pattern: str) -> list[int]:
    """PIDs whose command line contains `pattern`, excluding ourselves and any shell that merely mentions it."""
    out = subprocess.run(["/usr/bin/pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    pids = []
    for p in out:
        if not p.isdigit() or int(p) == os.getpid():
            continue
        cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", p], capture_output=True, text=True).stdout.strip()
        # a real lane process is `python -u tools/x.py ...`, not `zsh -c "... tools/x.py ..."`
        if cmd.startswith(("/bin/zsh", "/bin/bash", "zsh", "bash", "sh ", "/bin/sh")):
            continue
        pids.append(int(p))
    return pids


def manage_lanes(state: dict, level: str) -> None:
    lanes = state.setdefault("lanes", {})
    for lane in MANAGED_LANES:
        st = lanes.setdefault(lane["name"], {"red_ticks": 0, "starts": 0})
        pids = _lane_pids(lane["pattern"])
        st["pids"] = pids
        if not lane["enabled"]:
            st["status"] = "disabled"
            continue
        if level == "RED":
            st["red_ticks"] += 1
        else:
            st["red_ticks"] = 0
        if pids:
            if st["red_ticks"] >= RED_STOP_TICKS:
                for p in pids:
                    try:
                        os.kill(p, signal.SIGTERM)
                    except OSError:
                        pass
                st["status"] = "stopped_resource_red"; st["last_stop"] = _now().isoformat(timespec="seconds")
                _log(f"lane {lane['name']}: SIGTERM (resource RED x{st['red_ticks']})")
            else:
                st["status"] = "running"
            continue
        if LEVEL_RANK[level] > LEVEL_RANK[lane["max_level"]]:
            st["status"] = f"waiting_resource_{level}"
            continue
        os.makedirs(os.path.dirname(lane["log"]), exist_ok=True)
        with open(lane["log"], "a") as logf:
            subprocess.Popen(lane["cmd"], cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        st["starts"] += 1; st["status"] = "started"; st["last_start"] = _now().isoformat(timespec="seconds")
        _log(f"lane {lane['name']}: started ({' '.join(lane['cmd'])}) level={level}")


def run_healers(state: dict, level: str, tick: int) -> None:
    hs = state.setdefault("healers", {})
    for h in HEALERS:
        st = hs.setdefault(h["name"], {"runs": 0})
        if not h["enabled"]:
            st["status"] = "disabled"; continue
        if not h.get("always_run") and LEVEL_RANK[level] > LEVEL_RANK[h["max_level"]]:
            st["status"] = f"skipped_resource_{level}"; continue
        if tick % h["every_ticks"] != 0:
            continue
        if h.get("detach"):
            # long-running: launch detached, never wait (script self-locks + self-rate-limits)
            logf_path = os.path.join(LOGDIR, f"{h['name']}.out")
            os.makedirs(LOGDIR, exist_ok=True)
            with open(logf_path, "a") as logf:
                subprocess.Popen(h["cmd"], cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            st["runs"] += 1; st["last_run"] = _now().isoformat(timespec="seconds")
            st["status"] = "launched_detached"
            _log(f"healer {h['name']}: launched detached")
            continue
        rc, out = _run(h["cmd"], timeout=h.get("timeout", 120))
        st["runs"] += 1; st["last_run"] = _now().isoformat(timespec="seconds"); st["last_rc"] = rc
        st["status"] = "ran"
        _log(f"healer {h['name']}: rc={rc}")


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
            manage_lanes(state, level)
            run_healers(state, level, state["ticks"])
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
