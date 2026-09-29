#!/usr/bin/env python3
"""Unit tests for the bridge's batched insert path (--batch-insert).

Pins the handoff invariants with fakes -- no DB, no network:
  parity  the batch binds exactly what insert_scp_transaction binds, same column order
  I1      re-read rows count as `existing`, not `inserted`
  I2      one poison row cannot lose its batch (row-by-row replay)
  I3      session-level errors abort the drain; the watermark is held
  I4      the watermark only moves past committed batches, never past a held one
"""
import json, os, re, sys, tempfile
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/whatnot-sniper/tools"))
sys.path.insert(0, os.path.expanduser("~/whatnot-sniper"))
import psycopg
from psycopg.types.json import Jsonb
import bridge_scp_broad_to_neon as B
from mazi_db.scripts import trusted_enrichment_spine as TES

PASS = FAIL = 0
def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    PASS += ok; FAIL += (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   got={got!r} want={want!r}"))

IDLE = psycopg.pq.TransactionStatus.IDLE
INERROR = psycopg.pq.TransactionStatus.INERROR


def line(i, **kw):
    d = {"title": f"2024 Topps Now #D1 PSA 10 card {i}", "sold_price": "49.99",
         "sold_date": "2026-01-18", "source_item_id": f"scp_{i:040x}",
         "source_url": "https://www.sportscardspro.com/game/x/y", "slug": "x/y",
         "catalog_id": 123, "raw": {"scp_original_title": f"raw ebay title {i}", "slug": "x/y"}}
    d.update(kw)
    return (json.dumps(d) + "\n").encode()


def jsonl_of(lines, tail=b""):
    d = Path(tempfile.mkdtemp())
    j = d / "c.jsonl"
    j.write_bytes(b"".join(lines) + tail)
    st = d / "s.json"
    B.save_offset(st, 0, 0, 0)
    return j, st


def unwrap(p):
    return {k: (v.obj if isinstance(v, Jsonb) else v) for k, v in p.items()}


class Tx:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): self.conn.tx_opened += 1
    def __exit__(self, et, e, tb): return False   # re-raise, like psycopg


class FakeInfo:
    def __init__(self): self.transaction_status = IDLE


class FakeConn:
    def __init__(self, cur):
        self.info = FakeInfo(); self._cur = cur; self.tx_opened = 0
        self.inerror_after_batches = None
    def transaction(self): return Tx(self)
    def cursor(self, **kw): return self._cur
    def commit(self): pass
    def rollback(self): self.info.transaction_status = IDLE


class BatchCur:
    """Batch INSERT behaviour is scripted per call: an int = that many new rows,
    an exception instance = raised."""
    def __init__(self, script):
        self.script = list(script); self.batches = []; self._n = 0; self.conn = None
    def execute(self, sql, params=None):
        assert "DO NOTHING" in sql
        n_rows = sql.count("'USD'")
        self.batches.append(n_rows)
        act = self.script.pop(0) if self.script else n_rows
        if isinstance(act, BaseException):
            raise act
        self._n = act
        if self.conn and self.conn.inerror_after_batches == len(self.batches):
            self.conn.info.transaction_status = INERROR
    def fetchall(self): return [{"id": i} for i in range(self._n)]
    def close(self): pass


def fake_per_row(poison_ids=(), raise_on=None):
    calls = []
    def f(cur, row, run_id, target_id=None):
        calls.append(row.source_item_id)
        if raise_on and row.source_item_id in raise_on:
            raise raise_on[row.source_item_id]
        if row.source_item_id in poison_ids:
            raise psycopg.errors.UniqueViolation("poison")
        return 1
    return f, calls


def run(lines, script, batch_size=2, per_row=None, tail=b"", inerror_after=None, max_rows=None):
    j, st = jsonl_of(lines, tail)
    cur = BatchCur(script); conn = FakeConn(cur); cur.conn = conn
    conn.inerror_after_batches = inerror_after
    orig = B.insert_scp_transaction
    if per_row: B.insert_scp_transaction = per_row
    try:
        r = B.drain_once_batched(conn, j, st, 1, batch_size, max_rows)
        err = None
    except Exception as e:
        r, err = None, e
    finally:
        B.insert_scp_transaction = orig
    return r, err, B.load_offset(st), j, cur


print("── parity: batch binds exactly what insert_scp_transaction binds ──")
class RecCur:
    def __init__(self): self.calls = []
    def execute(self, sql, params=None): self.calls.append((sql, params))
    def fetchall(self): return []
    def fetchone(self): return {"id": 7} if "INSERT" in self.calls[-1][0] else None
for i, extra in enumerate([{}, {"raw": {"slug": "x/y"}}, {"image_url": "https://img/x.jpg"}]):
    row = B.row_from_jsonl(json.loads(line(i, **extra)))
    rc = RecCur()
    TES.insert_scp_transaction(rc, row, 42, None)
    per_row_sql, per_row_params = rc.calls[-1]
    check(f"row {i}: identical bound values", unwrap(B.scp_insert_params(row, 42)), unwrap(per_row_params))

cols = lambda sql: re.sub(r"\s+", " ", sql.split("external_transactions (", 1)[1].split(")", 1)[0]).strip()
bc = RecCur()
rows = [B.row_from_jsonl(json.loads(line(i))) for i in range(3)]
bc.fetchall = lambda: [{"id": 1}] * 3
B.insert_scp_batch(bc, rows, 42)
bsql, bparams = bc.calls[-1]
check("identical column list, same order", cols(bsql), cols(per_row_sql))
check("11 bound values per row", len(bparams), 33)
uw = lambda xs: [x.obj if isinstance(x, Jsonb) else x for x in xs]
check("row 2 slice == its per-row params", uw(bparams[22:33]),
      uw([B.scp_insert_params(rows[2], 42)[k] for k in B._SCP_COLS]))
check("arbiter repeats the partial-index predicate",
      "ON CONFLICT (source_code, source_item_id) WHERE source_item_id IS NOT NULL" in bsql, True)
check("empty batch sends nothing", B.insert_scp_batch(RecCur(), [], 1), 0)

print("── happy path: 5 rows, batch 2 -> 3 statements, watermark at EOF ──")
L = [line(i) for i in range(5)]
r, err, off, j, cur = run(L, [])
check("no error", err, None)
check("3 batch statements (2,2,1)", cur.batches, [2, 2, 1])
check("inserted 5", r["inserted"], 5)
check("offset == file size", off, j.stat().st_size)

print("── I1: a re-read counts as existing, not inserted ──")
r, err, off, j, cur = run(L, [2, 0, 1])
check("inserted 3", r["inserted"], 3)
check("existing 2", r["existing"], 2)
check("offset still advances to EOF", off, j.stat().st_size)

print("── I2: one poison row does not lose its batch ──")
pr, calls = fake_per_row(poison_ids={json.loads(L[1])["source_item_id"]})
r, err, off, j, cur = run(L, [psycopg.errors.UniqueViolation("title_date_price_uidx")], per_row=pr)
check("no error", err, None)
check("only the failed batch replayed row by row", len(calls), 2)
check("inserted 4 (1 replayed + 3 batched)", r["inserted"], 4)
check("skipped 1 (the poison row)", r["skipped"], 1)
check("offset at EOF", off, j.stat().st_size)

print("── statement_timeout replays instead of aborting ──")
pr, calls = fake_per_row()
r, err, off, j, cur = run(L, [psycopg.errors.QueryCanceled("timeout")], per_row=pr)
check("no error", err, None)
check("replayed the batch", len(calls), 2)
check("offset at EOF", off, j.stat().st_size)

print("── I3: session errors abort, watermark held, no replay ──")
for exc in (psycopg.errors.ReadOnlySqlTransaction("ro"), psycopg.errors.AdminShutdown("down"),
            psycopg.errors.CannotConnectNow("x"), psycopg.OperationalError("lost")):
    pr, calls = fake_per_row()
    r, err, off, j, cur = run(L, [2, exc], per_row=pr)
    name = type(exc).__name__
    check(f"{name}: raised", type(err), type(exc))
    check(f"{name}: no row-by-row replay", len(calls), 0)
    check(f"{name}: watermark held after batch 1", off, len(L[0]) + len(L[1]))

print("── I3 inside the replay: a session error mid-replay still aborts ──")
bad = json.loads(L[3])["source_item_id"]
pr, calls = fake_per_row(raise_on={bad: psycopg.errors.ReadOnlySqlTransaction("ro")})
r, err, off, j, cur = run(L, [2, psycopg.errors.UniqueViolation("u")], per_row=pr)
check("raised ReadOnlySqlTransaction", type(err), psycopg.errors.ReadOnlySqlTransaction)
check("watermark held at batch 1", off, len(L[0]) + len(L[1]))

print("── I4: INERROR at commit holds the watermark; the final flush cannot advance it ──")
r, err, off, j, cur = run(L, [], inerror_after=1)
check("no error", err, None)
check("offset NOT advanced", off, 0)
check("drain stopped after the held batch", cur.batches, [2])

print("── partial trailing line is never consumed ──")
r, err, off, j, cur = run(L[:3], [], tail=b'{"title": "half-writ')
check("offset stops before the partial line", off, sum(len(x) for x in L[:3]))
check("3 rows", r["processed"], 3)

print("── unparseable line is skipped, not fatal ──")
r, err, off, j, cur = run([L[0], b"{not json\n", L[2]], [])
check("skipped 1", r["skipped"], 1)
check("inserted 2", r["inserted"], 2)
check("offset at EOF", off, j.stat().st_size)

print("── --max stops at a line boundary and saves it ──")
r, err, off, j, cur = run(L, [], batch_size=500, max_rows=3)
check("processed 3", r["processed"], 3)
check("offset after row 3", off, sum(len(x) for x in L[:3]))

print(f"\n{'='*52}\nRESULT: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
