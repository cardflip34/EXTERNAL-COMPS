#!/usr/bin/env python3
"""C0 unit tests — NO NETWORK (PRD V2 §60). Run with the lane venv:
    .venv_extcomps/bin/python -m unittest external_engine/tests/test_c0_no_network.py -v
Covers: both eBay schema variants, price parsing ($, commas), best_offer bool/string, sold_date formats,
item-id extraction, same item discovered by 5 queries → 1 canonical row, price/date/image conflict flags,
graded row preferred, source-health state machine + governor deny, probe refusal before window + dry-run URL.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
ROOT = os.path.dirname(ENGINE)
sys.path.insert(0, ENGINE)
sys.path.insert(0, ROOT)

import duckdb  # noqa: E402
import canonical_migrate as cm  # noqa: E402
import source_health as sh  # noqa: E402


def _v1(item, title="2019 Topps Shohei Ohtani PSA 10", price="70.0", date="Mar 1, 2026", bo=False, src="ebay",
        pq="Shohei Ohtani", grader="PSA", grade="PSA 10", cid="PL-1", scraped="2026-04-11T08:35:20", img="https://i.ebayimg.com/a.webp"):
    return {"title": title, "sold_price": price, "sold_date": date, "condition": "Pre-Owned", "bids": "",
            "url": f"https://www.ebay.com/itm/{item}?_skw=shohei+ohtani+card&hash=x", "image_url": img,
            "best_offer": bo, "source": src, "player_query": pq, "grader": grader, "grade": grade,
            "comp_id": cid, "scraped_at": scraped}


def _v2(item, title="1992-93 Stadium Club Shaquille O Neal RC", price="1.25", date="2026-04-18", bo="False",
        pq="Shaquille O'Neal", cid="EB-693024", scraped="2026-04-18T10:09:46", img="https://i.ebayimg.com/b.webp"):
    return {"title": title, "price": price, "price_text": f"${price}", "sold_date": date, "sold_date_raw": f"Sold {date}",
            "condition": "Pre-Owned", "bids": "3 bids", "shipping": "+$1.32 delivery",
            "url": f"https://www.ebay.com/itm/{item}?_skw=Shaquille+O%27Neal+card", "image_url": img,
            "best_offer": bo, "item_id": str(item), "player_query": pq, "query_suffix": "card",
            "source_query": f"{pq} card", "price_range": "$1-$25", "comp_id": cid, "scraped_at": scraped}


class CanonicalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = duckdb.connect()
        self.con.execute("SET memory_limit='200MB'; SET threads=1")

    def tearDown(self):
        self.con.close(); self.tmp.cleanup()

    def _load(self, rows):
        # write a JSON array file, read with the same VARCHAR column contract as production
        p = os.path.join(self.tmp.name, "rows.json")
        with open(p, "w") as f:
            json.dump(rows, f)
        cols = ", ".join(f"'{k}': '{v}'" for k, v in cm.RAW_COLUMNS.items())
        src = (f"(SELECT *, 'rows.json' AS src_file, row_number() OVER () AS src_row "
               f"FROM read_json('{p}', format='array', records=true, columns={{{cols}}}))")
        self.con.execute(f"CREATE OR REPLACE TABLE normalized_t AS {cm.NORMALIZE_SQL.format(src=src)}")
        cm.run_canonical_stages(self.con, "normalized_t")
        return self.con

    def test_schema_v1_and_v2_normalize(self):
        con = self._load([_v1(100), _v2(200)])
        rows = {r[0]: r for r in con.execute(
            "SELECT observation_id, sold_price_usd, best_offer, sold_date, grade_company, shipping_text, discovery_sources, primary_subject_query FROM canonical_t").fetchall()}
        self.assertEqual(set(rows), {"EBAY:100", "EBAY:200"})
        v1, v2 = rows["EBAY:100"], rows["EBAY:200"]
        self.assertEqual(v1[1], 70.0); self.assertIs(v1[2], False); self.assertEqual(str(v1[3]), "2026-03-01")
        self.assertEqual(v1[4], "PSA"); self.assertIsNone(v1[5]); self.assertEqual(v1[6], ["ebay"])
        self.assertEqual(v2[1], 1.25); self.assertIs(v2[2], False); self.assertEqual(str(v2[3]), "2026-04-18")
        self.assertIsNone(v2[4]); self.assertEqual(v2[5], "+$1.32 delivery"); self.assertEqual(v2[6], ["ebay_v2_priority"])
        self.assertEqual(v2[7], "Shaquille O'Neal")

    def test_price_formats_and_best_offer_strings(self):
        con = self._load([_v1(1, price="$1,234.56", bo=True), _v2(2, price="16500.0", bo="True"),
                          _v1(3, price="", bo=None), _v2(4, price="abc", bo="maybe")])
        got = dict(con.execute("SELECT source_item_id, sold_price_usd FROM canonical_t").fetchall())
        self.assertEqual(got["1"], 1234.56); self.assertEqual(got["2"], 16500.0)
        self.assertIsNone(got["3"]); self.assertIsNone(got["4"])
        bo = dict(con.execute("SELECT source_item_id, best_offer FROM canonical_t").fetchall())
        self.assertIs(bo["1"], True); self.assertIs(bo["2"], True); self.assertIsNone(bo["3"]); self.assertIsNone(bo["4"])

    def test_sold_date_formats(self):
        con = self._load([_v1(1, date="Sold Apr 18, 2026"), _v1(2, date="September 3, 2025"), _v2(3, date="2026-06-15"),
                          _v1(4, date="garbage")])
        d = dict(con.execute("SELECT source_item_id, CAST(sold_date AS VARCHAR) FROM canonical_t").fetchall())
        self.assertEqual(d["1"], "2026-04-18"); self.assertEqual(d["2"], "2025-09-03"); self.assertEqual(d["3"], "2026-06-15")
        self.assertIsNone(d["4"])

    def test_same_item_five_queries_one_canonical_row(self):
        rows = [_v1(777, pq=q, src=s, cid=f"PL-{i}", scraped=f"2026-04-1{i}T00:00:00", grader="", grade="")
                for i, (q, s) in enumerate([("Kobe Bryant", "ebay_player_matrix"), ("Kobe Bryant card", "ebay_player_search"),
                                            ("1996 Topps Chrome", "ebay"), ("Kobe refractor", "priority_backfill"),
                                            ("Lakers", "ebay_player_matrix")])]
        rows.append(_v1(777, pq="Kobe Bryant", src="ebay_player_matrix", cid="PL-9", scraped="2026-05-01T00:00:00",
                        grader="PSA", grade="PSA 9"))  # graded duplicate should be the chosen row
        con = self._load(rows)
        r = con.execute("""SELECT n_observations, len(queries_seen), len(discovery_sources), grade_company,
                                  CAST(first_seen_at AS VARCHAR), CAST(last_seen_at AS VARCHAR), price_conflict, date_conflict, image_conflict
                           FROM canonical_t""").fetchall()
        self.assertEqual(len(r), 1)
        n, nq, ns, gc, first, last, pc, dc, ic = r[0]
        self.assertEqual(n, 6); self.assertEqual(nq, 5); self.assertEqual(ns, 4); self.assertEqual(gc, "PSA")
        self.assertTrue(first.startswith("2026-04-10")); self.assertTrue(last.startswith("2026-05-01"))
        self.assertFalse(pc); self.assertFalse(dc); self.assertFalse(ic)

    def test_conflict_flags_and_multiqty_rows(self):
        # v1.1: same item id, DIFFERENT sold dates = two legitimate sales (multi-quantity listing) → two rows
        con = self._load([_v1(5, price="10.0", date="Mar 1, 2026", img="https://i/x.webp"),
                          _v1(5, price="10.0", date="Mar 2, 2026", img="https://i/x.webp", cid="PL-2")])
        rows = con.execute("SELECT observation_key, n_observations, price_conflict FROM canonical_t ORDER BY 1").fetchall()
        self.assertEqual([r[0] for r in rows], ["EBAY:5:2026-03-01", "EBAY:5:2026-03-02"])
        self.assertEqual([r[1] for r in rows], [1, 1]); self.assertFalse(any(r[2] for r in rows))
        # same id, same date, different price/image = a true conflict on one sale
        con = self._load([_v1(6, price="10.0", date="Mar 1, 2026", img="https://i/x.webp"),
                          _v1(6, price="12.0", date="Mar 1, 2026", img="https://i/y.webp", cid="PL-2")])
        pc, ic, n = con.execute("SELECT price_conflict, image_conflict, n_observations FROM canonical_t").fetchone()
        self.assertTrue(pc); self.assertTrue(ic); self.assertEqual(n, 2)

    def test_valuation_gate(self):
        con = self._load([_v1(1, title="2023 Prizm Wembanyama Silver PSA 10"), _v1(2, bo=True),
                          _v1(3, title="2019 Select Zion U Pick Card Rookie"), _v1(4, title="Lot of 50 basketball cards"),
                          _v1(5, price="")])
        g = dict(con.execute("SELECT source_item_id, valuation_gate FROM canonical_t").fetchall())
        self.assertEqual(g, {"1": "ok", "2": "obo", "3": "ambiguous_pick", "4": "lot_bundle", "5": "no_price"})

    def test_raw_parquet_roundtrip_keeps_discovery_source(self):
        """Regression: a raw Parquet path containing 'source=ebay' made DuckDB's Hive auto-detection
        overwrite the real `source` column with 'ebay'. Raw reads must use hive_partitioning=false."""
        rows = [_v1(1, src="ebay_player_matrix"), _v1(2, src="priority_backfill"), _v2(3)]
        p = os.path.join(self.tmp.name, "rows.json")
        with open(p, "w") as f:
            json.dump(rows, f)
        cols = ", ".join(f"'{k}': '{v}'" for k, v in cm.RAW_COLUMNS.items())
        bad_dir = os.path.join(self.tmp.name, "raw_parquet", "source=ebay"); os.makedirs(bad_dir)
        pq = os.path.join(bad_dir, "rows.parquet")
        self.con.execute(f"COPY (SELECT *, 'rows.json' AS src_file, row_number() OVER () AS src_row "
                         f"FROM read_json('{p}', format='array', records=true, columns={{{cols}}})) TO '{pq}' (FORMAT PARQUET)")
        shadowed = self.con.execute(f"SELECT DISTINCT source FROM read_parquet('{pq}')").fetchall()
        self.assertEqual(shadowed, [("ebay",)])  # documents the trap
        src = f"read_parquet('{pq}', hive_partitioning=false)"
        self.con.execute(f"CREATE OR REPLACE TABLE normalized_t AS {cm.NORMALIZE_SQL.format(src=src)}")
        cm.run_canonical_stages(self.con, "normalized_t")
        got = dict(self.con.execute("SELECT source_item_id, discovery_sources[1] FROM canonical_t").fetchall())
        self.assertEqual(got, {"1": "ebay_player_matrix", "2": "priority_backfill", "3": "ebay_v2_priority"})

    def test_staged_nightly_rows_get_provenance_and_dedup(self):
        """PRD §53: staged nightly rows (no source/comp_id) are stamped nightly_cron + capture_date and dedup
        by item id against existing observations (late-arriving sale + already-known sale)."""
        known = [_v1(900, src="ebay_player_matrix", cid="PL-1")]
        night = [{k: v for k, v in _v1(900, cid="", src="").items() if k not in ("source", "comp_id")},
                 {k: v for k, v in _v1(901, cid="", src="", date="Jun 15, 2026").items() if k not in ("source", "comp_id")}]
        pk = os.path.join(self.tmp.name, "player_comps.json"); pn = os.path.join(self.tmp.name, "ebay_nightly_2026-06-15.raw.json")
        json.dump(known, open(pk, "w")); json.dump(night, open(pn, "w"))
        cols = ", ".join(f"'{k}': '{v}'" for k, v in cm.RAW_COLUMNS.items())
        outs = []
        for src in (pk, pn):
            dst = os.path.join(self.tmp.name, os.path.basename(src) + ".parquet"); outs.append(dst)
            self.con.execute(f"COPY ({cm.raw_select_clause(src)} FROM read_json('{src}', format='array', records=true, columns={{{cols}}})) TO '{dst}' (FORMAT PARQUET)")
        rel = "read_parquet([" + ", ".join(f"'{p}'" for p in outs) + "], union_by_name=true, hive_partitioning=false)"
        self.con.execute(f"CREATE OR REPLACE TABLE normalized_t AS {cm.NORMALIZE_SQL.format(src=rel)}")
        cm.run_canonical_stages(self.con, "normalized_t")
        rows = {r[0]: r for r in self.con.execute("SELECT source_item_id, n_observations, discovery_sources, CAST(sold_date AS VARCHAR) FROM canonical_t").fetchall()}
        self.assertEqual(rows["900"][1], 2); self.assertEqual(sorted(rows["900"][2]), ["ebay_player_matrix", "nightly_cron"])
        self.assertEqual(rows["901"][1], 1); self.assertEqual(rows["901"][2], ["nightly_cron"]); self.assertEqual(rows["901"][3], "2026-06-15")
        self.assertIn("nightly_cron", cm.raw_select_clause("/x/ebay_nightly_2026-06-15.graded.json"))
        self.assertNotIn("nightly_cron", cm.raw_select_clause("/x/player_comps.json"))

    def test_rerun_is_idempotent(self):
        rows = [_v1(1), _v1(1, cid="PL-2"), _v2(2)]
        con = self._load(rows)
        a = con.execute("SELECT count(*), sum(n_observations) FROM canonical_t").fetchone()
        cm.run_canonical_stages(con, "normalized_t")
        b = con.execute("SELECT count(*), sum(n_observations) FROM canonical_t").fetchone()
        self.assertEqual(a, b); self.assertEqual(a, (2, 3))


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_machine_and_governor(self):
        ok, _ = sh.contact_allowed("ebay", self.store); self.assertTrue(ok)           # default HEALTHY
        sh.set_state("ebay", "BLOCKED", "probe RED", self.store)
        ok, why = sh.contact_allowed("ebay", self.store); self.assertFalse(ok); self.assertIn("BLOCKED", why)
        for st in ("COOLDOWN", "AUTH_REQUIRED", "CONFIG_ERROR", "OFFLINE"):
            sh.set_state("ebay", st, "", self.store); self.assertFalse(sh.contact_allowed("ebay", self.store)[0])
        sh.set_state("ebay", "DEGRADED", "", self.store); self.assertTrue(sh.contact_allowed("ebay", self.store)[0])
        with self.assertRaises(ValueError):
            sh.set_state("ebay", "NOT_A_STATE", "", self.store)
        doc = sh.load("ebay", self.store)
        self.assertEqual(doc["state"], "DEGRADED"); self.assertGreaterEqual(len(doc["events"]), 6)

    def test_probe_refuses_before_window_and_dry_run(self):
        import ebay_gentle_probe as probe
        nb = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(timespec="seconds")
        sh.set_state("ebay", "BLOCKED", "t", self.store, probe_not_before=nb)
        self.assertEqual(probe.run(force=False, dry_run=False, store=self.store, timeout_ms=1), 4)  # refused, no network
        self.assertEqual(probe.run(force=False, dry_run=True, store=self.store, timeout_ms=1), 0)   # dry-run, no network
        self.assertIn("LH_Sold=1", probe.probe_url()); self.assertIn("LH_Complete=1", probe.probe_url())

    def test_classify(self):
        import ebay_gentle_probe as probe
        self.assertEqual(probe.classify("SORRY Something went wrong on our end", 0), "RED_BLOCKED")
        self.assertEqual(probe.classify("Please verify you are a robot", 0), "RED_CAPTCHA")
        # 2026-08 challenge wall: HTTP 200, no 'robot' token, 0 items — must NOT read as GREEN/served
        self.assertEqual(probe.classify("Please verify yourself to continue To keep eBay a safe place", 0), "RED_CAPTCHA")
        self.assertEqual(probe.classify("Shop by category 1,234 results", 60), "GREEN")
        self.assertEqual(probe.classify("0 results did not match any", 0), "AMBER_NO_RESULTS")


if __name__ == "__main__":
    unittest.main()
