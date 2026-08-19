#!/usr/bin/env python3
"""external_acquisition_ledger — durable run ledger with checkpoints (PRD V2 §31/§32).

SQLite (WAL) at <store>/external_store/ledger/external_acquisition_ledger.sqlite (falls back to
~/mazi_local_evidence/... when the volume is not writable). Stdlib only, so it runs under /usr/bin/python3.

A run is never "completed" just because a process exited: callers must call complete(); anything left in
STARTED/RUNNING after a crash shows up in find_incomplete() and can be resumed from its checkpoint.

API:
    L = Ledger()                         # default path
    run_id = L.start_run(source="ebay", lane="freshness", query_id="player:Shohei Ohtani",
                         subject="Shohei Ohtani", window_start="2026-08-18", window_end="2026-08-18",
                         code_version="v2.0.0")
    L.checkpoint(run_id, {"page": 3, "last_item": "1234"}, pages=3, rows_seen=120, rows_new=40, rows_duplicate=80)
    L.complete(run_id, rows_new=..., ...)           # status=completed
    L.fail(run_id, "403 blocked", retry=True)       # status=retry (or failed)
    L.find_incomplete(source="ebay")                # crashed/unfinished runs to resume
    L.window_done(source, query_id, window_start, window_end)   # True if a completed run covers it
    L.summary(since_hours=24)                        # counts for the dashboard/heartbeat
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from datetime import datetime, timezone

STORE = os.environ.get("MAZI_EBAY_SCRUB_STORE", "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_scrub_store")
HOME_FALLBACK = os.path.expanduser("~/mazi_local_evidence/ebay_scrub_store")
STATUSES = ("started", "running", "completed", "partial", "failed", "retry", "blocked", "exhausted", "unavailable")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT PRIMARY KEY,
  source         TEXT NOT NULL,
  lane           TEXT NOT NULL,           -- freshness | backfill | targeted | migration | probe
  query_id       TEXT,
  subject        TEXT,
  window_start   TEXT,                    -- ISO date/datetime
  window_end     TEXT,
  started_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  completed_at   TEXT,
  status         TEXT NOT NULL,
  pages          INTEGER DEFAULT 0,
  rows_seen      INTEGER DEFAULT 0,
  rows_new       INTEGER DEFAULT 0,
  rows_duplicate INTEGER DEFAULT 0,
  rows_rejected  INTEGER DEFAULT 0,
  errors         INTEGER DEFAULT 0,
  last_error     TEXT,
  retry_count    INTEGER DEFAULT 0,
  checkpoint     TEXT,                    -- JSON
  machine        TEXT,
  code_version   TEXT,
  extra          TEXT                     -- JSON (free-form provenance)
);
CREATE INDEX IF NOT EXISTS runs_source_status ON runs(source, status);
CREATE INDEX IF NOT EXISTS runs_window ON runs(source, query_id, window_start, window_end, status);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_path(store: str | None = None) -> str:
    base = store or STORE
    d = os.path.join(base, "external_store", "ledger")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".w"), "w") as f:
            f.write("1")
        os.remove(os.path.join(d, ".w"))
    except OSError:
        d = os.path.join(HOME_FALLBACK, "external_store", "ledger")
        os.makedirs(d, exist_ok=True)
    return os.path.join(d, "external_acquisition_ledger.sqlite")


class Ledger:
    def __init__(self, path: str | None = None, store: str | None = None):
        self.path = path or default_path(store)
        self.con = sqlite3.connect(self.path, timeout=30, isolation_level=None)  # autocommit
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(SCHEMA)
        self.con.row_factory = sqlite3.Row

    # --- lifecycle -------------------------------------------------------------------------
    def start_run(self, source: str, lane: str, query_id: str | None = None, subject: str | None = None,
                  window_start: str | None = None, window_end: str | None = None, code_version: str | None = None,
                  extra: dict | None = None, run_id: str | None = None) -> str:
        run_id = run_id or f"{source}-{lane}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        now = _now()
        self.con.execute(
            "INSERT INTO runs(run_id, source, lane, query_id, subject, window_start, window_end, started_at, updated_at, "
            "status, machine, code_version, extra) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, source, lane, query_id, subject, window_start, window_end, now, now, "started",
             socket.gethostname(), code_version, json.dumps(extra) if extra else None))
        return run_id

    def checkpoint(self, run_id: str, checkpoint: dict | None = None, **counters) -> None:
        sets, vals = ["status='running'", "updated_at=?"], [_now()]
        if checkpoint is not None:
            sets.append("checkpoint=?"); vals.append(json.dumps(checkpoint))
        for k in ("pages", "rows_seen", "rows_new", "rows_duplicate", "rows_rejected", "errors"):
            if k in counters and counters[k] is not None:
                sets.append(f"{k}=?"); vals.append(int(counters[k]))
        vals.append(run_id)
        self.con.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", vals)

    def complete(self, run_id: str, status: str = "completed", **counters) -> None:
        if status not in STATUSES:
            raise ValueError(status)
        sets, vals = ["status=?", "completed_at=?", "updated_at=?"], [status, _now(), _now()]
        for k in ("pages", "rows_seen", "rows_new", "rows_duplicate", "rows_rejected", "errors"):
            if k in counters and counters[k] is not None:
                sets.append(f"{k}=?"); vals.append(int(counters[k]))
        vals.append(run_id)
        self.con.execute(f"UPDATE runs SET {', '.join(sets)} WHERE run_id=?", vals)

    def fail(self, run_id: str, error: str, retry: bool = True, blocked: bool = False) -> None:
        status = "blocked" if blocked else ("retry" if retry else "failed")
        self.con.execute(
            "UPDATE runs SET status=?, last_error=?, errors=errors+1, retry_count=retry_count+?, updated_at=? WHERE run_id=?",
            (status, str(error)[:2000], 1 if retry else 0, _now(), run_id))

    # --- queries ---------------------------------------------------------------------------
    def get(self, run_id: str) -> dict | None:
        r = self.con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def find_incomplete(self, source: str | None = None, lane: str | None = None, older_than_minutes: int = 0) -> list[dict]:
        q = "SELECT * FROM runs WHERE status IN ('started','running','retry','partial')"
        args: list = []
        if source:
            q += " AND source=?"; args.append(source)
        if lane:
            q += " AND lane=?"; args.append(lane)
        if older_than_minutes:
            q += " AND updated_at < ?"
            from datetime import timedelta
            args.append((datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat(timespec="seconds"))
        q += " ORDER BY started_at"
        return [dict(r) for r in self.con.execute(q, args).fetchall()]

    def window_done(self, source: str, query_id: str, window_start: str, window_end: str) -> bool:
        r = self.con.execute(
            "SELECT 1 FROM runs WHERE source=? AND query_id=? AND window_start=? AND window_end=? AND status='completed' LIMIT 1",
            (source, query_id, window_start, window_end)).fetchone()
        return r is not None

    def summary(self, since_hours: int = 24) -> dict:
        from datetime import timedelta
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat(timespec="seconds")
        rows = self.con.execute(
            "SELECT source, lane, status, count(*) n, sum(rows_new) rn, sum(rows_duplicate) rd, sum(rows_rejected) rr, sum(errors) e "
            "FROM runs WHERE started_at >= ? GROUP BY source, lane, status", (since,)).fetchall()
        last_ok = self.con.execute("SELECT source, max(completed_at) FROM runs WHERE status='completed' GROUP BY source").fetchall()
        return {"since_hours": since_hours,
                "by_source_lane_status": [dict(r) for r in rows],
                "last_completed_by_source": {r[0]: r[1] for r in last_ok}}

    def close(self) -> None:
        self.con.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["summary", "incomplete", "path"])
    ap.add_argument("--hours", type=int, default=24)
    a = ap.parse_args()
    L = Ledger()
    if a.cmd == "path":
        print(L.path)
    elif a.cmd == "summary":
        print(json.dumps(L.summary(a.hours), indent=2))
    else:
        print(json.dumps(L.find_incomplete(), indent=2))
