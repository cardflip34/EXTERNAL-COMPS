#!/usr/bin/env python3
"""
sources_supervisor.py — External Comps "don't stop" loop (non-eBay sources)
============================================================================
Keeps the multi-source scrub running autonomously (operator directive
2026-07-10: "keep scrubbing all possible sources autonomously, don't stop"):

  every cycle (10 min):
    - fanatics KEEPALIVE: relaunch fanatics_full_catalog_scraper_v3.py if dead
      (it dies on DNS/network blips every few days; resume-from-state is safe)
  every AR_EVERY_H (6h):
    - auctionreport_scraper.py (RSS watermark -> idempotent)
  every GOLDIN_EVERY_H (24h):
    - goldin_scraper_v2.py --closed-in <today-30d>..<tomorrow>: EVERY auction that
      ended in the window and is not yet fully captured (state cursor -> idempotent).
      Was "--newest-auctions 4" once a week, which missed 87 of 2026's auctions
      (nearly all Elite, e.g. the June Exquisite LeBron $2.93M) -- 2026-09-29.
  every BRIDGE_EVERY_H (24h):
    - import_fanatics_v3_chunks_to_neon (own lock, byte-offset checkpoint so each
      run reads only lines added since its last commit, ON CONFLICT dedup) so
      fresh chunks keep landing in Neon
  every cycle:
    - heartbeat -> ~/Library/Logs/mazi_external_comps/sources_heartbeat.json

Deliberately NO eBay anywhere: the eBay roster/supervisor stays parked until the
soft-block clears (separate gentle-probe protocol). Launch detached from an ssh
session (nohup) so the fanatics/bridge legs keep Full Disk Access to the 6TB
store — do NOT convert to cron/launchd (TCC would break those legs).

  cd ~/whatnot-sniper && nohup python3 -u tools/sources_supervisor.py \
      >> ~/Library/Logs/mazi_external_comps/sources_supervisor.out 2>&1 &
"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, date, timedelta

ROOT = os.path.expanduser("~/whatnot-sniper")
LOGDIR = os.path.expanduser("~/Library/Logs/mazi_external_comps")
HEARTBEAT = os.path.join(LOGDIR, "sources_heartbeat.json")
FANATICS_OUT = os.path.join(LOGDIR, "fanatics.out")
FANATICS_STATE = ("/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/"
                  "fanatics_full_catalog_v3/fanatics_full_catalog_v3_state.json")
ENV_FILE = os.path.join(ROOT, ".env.external_comps_bridge")

CYCLE_SLEEP = int(os.environ.get("SRC_SUP_CYCLE_SLEEP", "600"))
AR_EVERY_H = float(os.environ.get("SRC_SUP_AR_EVERY_H", "6"))
# RETIRED 2026-10-03 (Andy): auctionreport.com's RSS has answered HTTP 403 since 2026-09-13, so every 6 h leg failed
# (rc=1) and added nothing. Its 398 rows stay in Neon. SRC_SUP_AR=1 turns the leg back on.
AR_ENABLED = os.environ.get("SRC_SUP_AR", "0") == "1"
BRIDGE_EVERY_H = float(os.environ.get("SRC_SUP_BRIDGE_EVERY_H", "24"))
GOLDIN_EVERY_H = float(os.environ.get("SRC_SUP_GOLDIN_EVERY_H", "24"))
GOLDIN_WINDOW_DAYS = int(os.environ.get("SRC_SUP_GOLDIN_WINDOW_DAYS", "30"))
MAX_FANATICS_RELAUNCH_PER_DAY = int(os.environ.get("SRC_SUP_MAX_RELAUNCH", "6"))
REFRESH_EVERY_H = float(os.environ.get("SRC_SUP_REFRESH_EVERY_H", "6"))
TCG_EVERY_H = float(os.environ.get("SRC_SUP_TCG_EVERY_H", "12"))
MYSLABS_EVERY_H = float(os.environ.get("SRC_SUP_MYSLABS_EVERY_H", "6"))
LOCAL_BRIDGE_EVERY_H = float(os.environ.get("SRC_SUP_LOCAL_BRIDGE_EVERY_H", "12"))
REA_EVERY_H = float(os.environ.get("SRC_SUP_REA_EVERY_H", "24"))
LOTSGALLERY_EVERY_H = float(os.environ.get("SRC_SUP_LOTSGALLERY_EVERY_H", "168"))

_stop = False


def _sig(_s, _f):
    global _stop
    _stop = True


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def load_env_file(path):
    """Parse KEY=VALUE lines (the bridge env file) into a dict."""
    env = {}
    try:
        for ln in open(path):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


def cdp_up(url):
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=5).read()
        return True
    except Exception:
        return False


def pgrep(pattern):
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True,
                             text=True, timeout=30)
        pids = [p for p in out.stdout.split() if p.strip().isdigit()]
        return [int(p) for p in pids if int(p) != os.getpid()]
    except Exception:
        return []


def load_state():
    try:
        return json.load(open(HEARTBEAT))
    except Exception:
        return {}


def write_heartbeat(state):
    state["ts"] = now_iso()
    tmp = HEARTBEAT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, HEARTBEAT)


def hours_since(iso_ts):
    if not iso_ts:
        return 1e9
    try:
        then = datetime.fromisoformat(iso_ts)
        return (datetime.now() - then).total_seconds() / 3600.0
    except Exception:
        return 1e9


def _tail_text(text, lines=4, width=160):
    """Last few lines of captured output (TimeoutExpired hands back bytes even with text=True)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return " | ".join(t[:width] for t in (text or "").strip().splitlines()[-lines:])


def _record_leg_health(state, key, started):
    """Per-leg consecutive-failure streak. external_engine/freshness_report.py alerts on it, because
    sold-date lag only crosses its threshold days after a feed breaks (fanatics read "5d ok" on
    2026-09-14 after three straight bridge failures)."""
    health = state.setdefault("leg_health", {}).setdefault(key, {"consecutive_failures": 0})
    if state["last_" + key]["rc"] == 0:
        health["consecutive_failures"] = 0
        health["last_ok_at"] = started
    else:
        health["consecutive_failures"] = health.get("consecutive_failures", 0) + 1
        health["last_fail_at"] = started


def run_leg(state, key, cmd, timeout_s, extra_env=None, cwd=ROOT):
    """Run one scrub/bridge leg synchronously with a timeout; record outcome."""
    print("[%s] LEG %s: %s" % (now_iso(), key, " ".join(cmd)[:140]), flush=True)
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    started = now_iso()
    try:
        res = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                             text=True, timeout=timeout_s)
        tail = (res.stdout or "").strip().splitlines()[-3:]
        state["last_" + key] = {"at": started, "rc": res.returncode,
                                "tail": " | ".join(t[:110] for t in tail)}
        print("[%s] LEG %s done rc=%s %s" % (now_iso(), key, res.returncode,
                                             state["last_" + key]["tail"][:200]), flush=True)
        if res.returncode != 0:
            # The traceback is on stderr; without it a failed leg logged a bare "rc=1"
            # (the 2026-09-11 fanatics_bridge failure left no cause at all).
            state["last_" + key]["err"] = _tail_text(res.stderr)
            print("[%s] LEG %s stderr: %s" % (now_iso(), key, state["last_" + key]["err"]), flush=True)
    except subprocess.TimeoutExpired as exc:
        state["last_" + key] = {"at": started, "rc": "timeout", "tail": "",
                                "err": _tail_text(exc.stderr)}
        print("[%s] LEG %s TIMEOUT after %ss %s" % (now_iso(), key, timeout_s,
                                                    state["last_" + key]["err"]), flush=True)
    except Exception as exc:
        state["last_" + key] = {"at": started, "rc": "error",
                                "tail": str(exc)[:160]}
        print("[%s] LEG %s ERROR %s" % (now_iso(), key, exc), flush=True)
    _record_leg_health(state, key, started)


def _fanatics_catalog_complete():
    """True once the full-catalog crawl has finished. A completed crawl must NOT
    be relaunched: re-running only re-verifies already-done shards (all in
    completed_shards, so nothing new is scraped) then exits within minutes —
    which the bare pgrep keepalive misreads as a crash and burns the daily
    relaunch cap ('operator attention' false alarm). Capturing NEW sales needs a
    separate refresh leg (reset recent completed_shards), not a keepalive."""
    try:
        with open(FANATICS_STATE) as f:
            st = json.load(f)
    except Exception:
        return False
    return bool(st.get("completed")) and not (st.get("discovery_queue_len") or 0)


def fanatics_keepalive(state):
    alive = pgrep("fanatics_full_catalog_scraper_v3")
    state["fanatics_alive"] = bool(alive)
    state["fanatics_pid"] = alive[0] if alive else None
    if alive:
        state.pop("fanatics_note", None)
        return
    if _fanatics_catalog_complete():
        state["fanatics_note"] = ("catalog crawl COMPLETE — not relaunching "
                                  "(done, not dead; freshness = separate refresh leg)")
        return
    today = date.today().isoformat()
    rl = state.get("fanatics_relaunches", {})
    n_today = rl.get(today, 0)
    if n_today >= MAX_FANATICS_RELAUNCH_PER_DAY:
        state["fanatics_note"] = "relaunch cap hit (%d today) — operator attention" % n_today
        print("[%s] fanatics DEAD but relaunch cap hit (%d today)" % (now_iso(), n_today), flush=True)
        return
    print("[%s] fanatics DEAD — relaunching (resume from state, #%d today)" % (
        now_iso(), n_today + 1), flush=True)
    with open(FANATICS_OUT, "a") as out:
        subprocess.Popen(
            [sys.executable, "-u", "fanatics_full_catalog_scraper_v3.py"],
            cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
    state["fanatics_relaunches"] = {today: n_today + 1}
    state["fanatics_last_relaunch"] = now_iso()


def main():
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    os.makedirs(LOGDIR, exist_ok=True)
    one_cycle = "--once" in sys.argv
    dry = "--dry-run" in sys.argv
    state = load_state()
    state.setdefault("started_at", now_iso())
    state["supervisor_pid"] = os.getpid()
    bridge_env = load_env_file(ENV_FILE)
    bridge_env["MAZI_DB_NO_POOL"] = "1"
    bridge_env["PYTHONPATH"] = ROOT
    print("[%s] sources_supervisor start pid=%d cycle=%ds ar=%.0fh bridge=%.0fh goldin=%.0fh/%dd dry=%s" % (
        now_iso(), os.getpid(), CYCLE_SLEEP, AR_EVERY_H, BRIDGE_EVERY_H, GOLDIN_EVERY_H, GOLDIN_WINDOW_DAYS, dry), flush=True)

    cycle = 0
    while not _stop:
        cycle += 1
        state["cycle"] = cycle

        # 1) fanatics keepalive
        if dry:
            state["fanatics_alive"] = bool(pgrep("fanatics_full_catalog_scraper_v3"))
        else:
            fanatics_keepalive(state)

        # 2) auctionreport (6h) — skip if an instance is already running
        if AR_ENABLED and hours_since((state.get("last_auctionreport") or {}).get("at")) >= AR_EVERY_H:
            if dry:
                print("[dry] would run auctionreport_scraper.py", flush=True)
            elif pgrep("auctionreport_scraper.py"):
                print("[%s] auctionreport already running — skip" % now_iso(), flush=True)
            else:
                run_leg(state, "auctionreport",
                        [sys.executable, "-u", "auctionreport_scraper.py"], 1800)

        # 3) goldin (daily) -- every auction that ENDED in the last GOLDIN_WINDOW_DAYS and is not yet fully captured.
        # Goldin runs Elite and specialty auctions on any day, not just the Thursday weekly; the scraper's state skips
        # auctions already covered, so re-reading a 30-day window costs one calendar load when nothing is new.
        if hours_since((state.get("last_goldin") or {}).get("at")) >= GOLDIN_EVERY_H:
            today = date.today()
            window = "%s..%s" % ((today - timedelta(days=GOLDIN_WINDOW_DAYS)).isoformat(),
                                 (today + timedelta(days=1)).isoformat())
            if dry:
                print("[dry] would run goldin_scraper_v2.py --closed-in %s" % window, flush=True)
            elif pgrep("goldin_scraper_v2"):
                # single-instance guard: goldin_v2 has no file lock; two writers
                # would race on goldin_comps_v2.json/state
                print("[%s] goldin already running — skip this cycle" % now_iso(), flush=True)
            else:
                # 200 pages x 240 lots: an Elite/Premier close can run past the old 25-page (6,000-lot) cap
                run_leg(state, "goldin",
                        [sys.executable, "-u", "goldin_scraper_v2.py",
                         "--closed-in", window, "--max-pages-per-auction", "200"],
                        3 * 3600)

        # 4) fanatics RECENT REFRESH (6h) — the catalog crawl is complete, so this
        # captures NEW sales (soldDate,desc, shallow) into fresh chunks; the bridge
        # below lands them. Skip if any fanatics writer is already running.
        if hours_since((state.get("last_fanatics_refresh") or {}).get("at")) >= REFRESH_EVERY_H:
            if dry:
                print("[dry] would run fanatics_recent_refresh.py --pages 5", flush=True)
            elif pgrep("fanatics_recent_refresh.py") or pgrep("fanatics_full_catalog_scraper_v3"):
                print("[%s] fanatics writer already running — skip refresh" % now_iso(), flush=True)
            else:
                run_leg(state, "fanatics_refresh",
                        [sys.executable, "-u", "fanatics_recent_refresh.py", "--pages", "5"],
                        2 * 3600)

        # 5) fanatics chunk bridge (24h) — needs bridge env; own lock; dedup-safe
        if hours_since((state.get("last_fanatics_bridge") or {}).get("at")) >= BRIDGE_EVERY_H:
            if dry:
                print("[dry] would run import_fanatics_v3_chunks_to_neon", flush=True)
            else:
                run_leg(state, "fanatics_bridge",
                        [sys.executable, "-m",
                         "mazi_db.scripts.import_fanatics_v3_chunks_to_neon",
                         "--target", "prod", "--commit", "--yes-i-understand-prod",
                         # 0, not 20: the importer never consumes a partial trailing line, and this leg
                         # starts right after the refresh leg, so a 20-min skip dropped the chunk it had
                         # just written (09-14: chunk 152 skipped, 1.5 min old).
                         "--skip-active-minutes", "0", "--batch-size", "500"],
                        4 * 3600, extra_env=bridge_env)

        # 6) tcgplayer sold comps (12h) — Pokemon TCG singles, local JSON
        if hours_since((state.get("last_tcgplayer") or {}).get("at")) >= TCG_EVERY_H:
            if dry:
                print("[dry] would run tcgplayer_sold_scraper.py", flush=True)
            elif pgrep("tcgplayer_sold_scraper.py"):
                print("[%s] tcgplayer already running — skip" % now_iso(), flush=True)
            else:
                run_leg(state, "tcgplayer",
                        [sys.executable, "-u", "tcgplayer_sold_scraper.py",
                         "--groups", "12", "--max-products", "400"], 3 * 3600)

        # 7) myslabs sold comps (6h) — graded singles archive, local JSON
        if hours_since((state.get("last_myslabs") or {}).get("at")) >= MYSLABS_EVERY_H:
            if dry:
                print("[dry] would run myslabs_scraper_v2.py", flush=True)
            elif pgrep("myslabs_scraper_v2.py"):
                print("[%s] myslabs already running — skip" % now_iso(), flush=True)
            else:
                run_leg(state, "myslabs",
                        [sys.executable, "-u", "myslabs_scraper_v2.py", "--scrolls", "12"], 1800)

        # 8) REA realized-price comps (24h) — collectrea.com archive (plain HTTP),
        # local JSON; the bridge leg below lands it.
        if hours_since((state.get("last_rea") or {}).get("at")) >= REA_EVERY_H:
            if dry:
                print("[dry] would run rea_scraper.py --by-auction --since-year %d" % (date.today().year - 1), flush=True)
            elif pgrep("rea_scraper.py"):
                print("[%s] rea already running — skip" % now_iso(), flush=True)
            else:
                run_leg(state, "rea",
                        [sys.executable, "-u", "rea_scraper.py", "--by-auction",
                         # every auction of this year and last, full depth (the top-40-pages listing only ever
                         # reached ~970 $50K+ lots) -- 2026-09-29
                         "--since-year", str(date.today().year - 1)], 2400)

        # 8b) Huggins & Scott (24h) -- same archive platform as REA; the pgrep guard keeps the two from overlapping
        if hours_since((state.get("last_hugginsandscott") or {}).get("at")) >= REA_EVERY_H:
            if dry:
                print("[dry] would run rea_scraper.py --house hugginsandscott --by-auction", flush=True)
            elif pgrep("rea_scraper.py"):
                print("[%s] rea_scraper already running — skip huggins & scott" % now_iso(), flush=True)
            else:
                run_leg(state, "hugginsandscott",
                        [sys.executable, "-u", "rea_scraper.py", "--house", "hugginsandscott", "--by-auction",
                         "--since-year", str(date.today().year - 1)], 2400)

        # 8c) Memory Lane / Lelands (weekly) -- new auctions only (done ones are in <house>_state.json). Cloudflare blocks
        # headless, so this needs the dedicated real Chrome on 127.0.0.1:9335 (see lotsgallery_scraper.py); without it the
        # leg is skipped with a warning, never started headless.
        for house in ("memorylane", "lelands"):
            if hours_since((state.get("last_" + house) or {}).get("at")) < LOTSGALLERY_EVERY_H:
                continue
            if dry:
                print("[dry] would run lotsgallery_scraper.py --house %s" % house, flush=True)
            elif pgrep("lotsgallery_scraper.py"):
                print("[%s] lotsgallery_scraper already running — skip %s" % (now_iso(), house), flush=True)
            elif not cdp_up("http://127.0.0.1:9335/json/version"):
                print("[%s] WARNING: auction Chrome (127.0.0.1:9335) is not open — %s skipped" % (now_iso(), house), flush=True)
            else:
                run_leg(state, house, [sys.executable, "-u", "lotsgallery_scraper.py", "--house", house], 4 * 3600)

        # 9) bridge local sources -> Neon (12h) — land tcgplayer/myslabs/AR/rea +
        # goldin incrementals continuously (idempotent ON CONFLICT DO NOTHING). Bridge env.
        if hours_since((state.get("last_local_bridge") or {}).get("at")) >= LOCAL_BRIDGE_EVERY_H:
            if dry:
                print("[dry] would run bridge_local_sources_to_neon.py --source all --commit", flush=True)
            elif pgrep("bridge_local_sources_to_neon.py"):
                print("[%s] local bridge already running — skip" % now_iso(), flush=True)
            else:
                run_leg(state, "local_bridge",
                        [sys.executable, "-u", "bridge_local_sources_to_neon.py",
                         "--source", "all", "--commit", "--yes"],
                        # 2 h, not 30 min: a backfill delta (Goldin 2012-2025, ~290K rows) needs ~50 min; the bridge
                        # now commits every 10K rows, so a kill keeps what landed
                        7200, extra_env=bridge_env)

        write_heartbeat(state)
        if one_cycle:
            print("[%s] --once: exiting after 1 cycle" % now_iso(), flush=True)
            break
        for _ in range(CYCLE_SLEEP):
            if _stop:
                break
            time.sleep(1)

    write_heartbeat(state)
    print("[%s] sources_supervisor clean exit" % now_iso(), flush=True)


if __name__ == "__main__":
    main()
