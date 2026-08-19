#!/usr/bin/env python3
"""C0 tests (no network) for the run ledger and the canonical writer.
    .venv_extcomps/bin/python -m unittest external_engine/tests/test_c0_writer_ledger.py -v
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__)); ENGINE = os.path.dirname(HERE)
sys.path.insert(0, ENGINE); sys.path.insert(0, HERE)

import duckdb  # noqa: E402
from ledger import Ledger  # noqa: E402
from canonical_writer import CanonicalWriter, WriterBusy  # noqa: E402
from test_c0_no_network import _v1, _v2  # noqa: E402


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.L = Ledger(path=os.path.join(self.tmp.name, "l.sqlite"))

    def tearDown(self):
        self.L.close(); self.tmp.cleanup()

    def test_lifecycle_checkpoint_resume_window(self):
        rid = self.L.start_run("ebay", "freshness", query_id="player:x", window_start="2026-08-18", window_end="2026-08-18", code_version="t")
        self.assertEqual(self.L.get(rid)["status"], "started")
        self.L.checkpoint(rid, {"page": 2}, pages=2, rows_seen=50, rows_new=10, rows_duplicate=40)
        r = self.L.get(rid); self.assertEqual(r["status"], "running"); self.assertEqual(json.loads(r["checkpoint"]), {"page": 2})
        self.assertEqual([x["run_id"] for x in self.L.find_incomplete("ebay")], [rid])   # crash would leave it here
        self.assertFalse(self.L.window_done("ebay", "player:x", "2026-08-18", "2026-08-18"))
        self.L.complete(rid, rows_new=12)
        self.assertTrue(self.L.window_done("ebay", "player:x", "2026-08-18", "2026-08-18"))
        self.assertEqual(self.L.find_incomplete("ebay"), [])
        rid2 = self.L.start_run("ebay", "backfill", query_id="player:y")
        self.L.fail(rid2, "403 Error Page", retry=False, blocked=True)
        self.assertEqual(self.L.get(rid2)["status"], "blocked")
        s = self.L.summary(24); self.assertIn("ebay", s["last_completed_by_source"])
        with self.assertRaises(ValueError):
            self.L.complete(rid, status="nonsense")


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = os.path.join(self.tmp.name, "store"); os.makedirs(self.store)
        self.out = os.path.join(self.store, "external_store")
        self.w = CanonicalWriter(store=self.store, out=self.out, memory_limit="200MB")

    def tearDown(self):
        self.w.close(); self.tmp.cleanup()

    def _stage(self, rows, name="stage.json"):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w") as f:
            json.dump(rows, f)
        return p

    def _canon_count(self):
        con = duckdb.connect()
        try:
            return con.execute(f"SELECT count(*) FROM read_parquet('{self.w.canon_dir}/**/*.parquet', hive_partitioning=true)").fetchone()[0]
        finally:
            con.close()

    def test_ingest_dedup_idempotent_multiqty_and_ledger(self):
        p1 = self._stage([_v1(1), _v1(2, src=""), _v2(3)], "a.json")
        r1 = self.w.ingest(p1, source_label="ebay_player_matrix", lane="freshness", query_id="q1", window_start="2026-08-18", window_end="2026-08-18")
        self.assertEqual((r1["rows_seen"], r1["rows_new"], r1["rows_duplicate"]), (3, 3, 0))
        self.assertEqual(self._canon_count(), 3)
        r2 = self.w.ingest(p1, source_label="ebay_player_matrix", lane="freshness", query_id="q1")      # same file again
        self.assertEqual((r2["rows_seen"], r2["rows_new"], r2["rows_duplicate"]), (3, 0, 3))
        self.assertEqual(self._canon_count(), 3)                                                       # zero duplicates added
        # known id, same date, different query → duplicate; known id, NEW sold date → multi-qty sale accepted
        p3 = self._stage([_v1(1, pq="Other Query"), _v1(1, date="Mar 9, 2026"), _v1(4)], "c.json")
        r3 = self.w.ingest(p3, source_label="ebay", lane="backfill", query_id="q2")
        self.assertEqual((r3["rows_new"], r3["rows_duplicate"]), (2, 1))
        self.assertEqual(self._canon_count(), 5)
        # stamped source label lands in discovery_sources; v2 row normalised (price 1.25)
        con = duckdb.connect()
        rows = con.execute(f"SELECT source_item_id, discovery_sources[1], sold_price_usd FROM read_parquet('{self.w.canon_dir}/**/*.parquet', hive_partitioning=true) ORDER BY 1").fetchall()
        con.close()
        d = {r[0]: r for r in rows}
        self.assertEqual(d["2"][1], "ebay_player_matrix"); self.assertEqual(d["3"][2], 1.25)
        # ledger has 3 completed runs with correct counters and window_done
        self.assertTrue(self.w.ledger.window_done("ebay", "q1", "2026-08-18", "2026-08-18"))
        self.assertEqual(len(self.w.ledger.find_incomplete()), 0)
        # index rebuild from parquet gives the same size
        self.assertEqual(self.w.ensure_index(rebuild=True), 5)

    def test_lock_busy_is_reported_and_ledgered(self):
        p = self._stage([_v1(10)], "b.json")
        other = CanonicalWriter(store=self.store, out=self.out, memory_limit="200MB")
        other.acquire()
        try:
            with self.assertRaises(WriterBusy):
                self.w.ingest(p, source_label="ebay")
        finally:
            other.close()
        inc = self.w.ledger.find_incomplete()
        self.assertEqual(len(inc), 1); self.assertEqual(inc[0]["status"], "retry"); self.assertIn("busy", inc[0]["last_error"])
        r = self.w.ingest(p, source_label="ebay")           # lock released → works
        self.assertEqual(r["rows_new"], 1)

    def test_dry_run_writes_nothing(self):
        p = self._stage([_v1(20), _v1(21)], "d.json")
        r = self.w.ingest(p, source_label="ebay", dry_run=True)
        self.assertEqual(r["rows_new"], 2); self.assertEqual(r["written_files"], 0)
        self.assertFalse(any(f.endswith(".parquet") for _, _, fs in os.walk(self.w.canon_dir) for f in fs))


if __name__ == "__main__":
    unittest.main()
