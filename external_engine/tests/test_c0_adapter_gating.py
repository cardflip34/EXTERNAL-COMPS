#!/usr/bin/env python3
"""C0 (no network): polite adapter gating + helpers; lane refusal while BLOCKED."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__)); ENGINE = os.path.dirname(HERE)
sys.path.insert(0, ENGINE); sys.path.insert(0, HERE)

import source_health as sh  # noqa: E402
import ebay_polite_adapter as epa  # noqa: E402


class AdapterGatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_check_blocked_then_degraded_and_budget(self):
        ad = epa.EbayPoliteAdapter(store=self.store, daily_budget=2)
        sh.set_state("ebay", "BLOCKED", "t", self.store)
        ok, why = ad.health_check(); self.assertFalse(ok); self.assertIn("BLOCKED", why)
        sh.set_state("ebay", "DEGRADED", "probe GREEN", self.store)
        ok, why = ad.health_check()
        # may be False only if the host is resource RED right now; either way the reason must not be the source state
        self.assertNotIn("BLOCKED", why)
        ad._count_request(); ad._count_request()
        self.assertEqual(ad.rate_budget()["used_today"], 2)
        ok, why = ad.health_check()
        if "resource RED" not in why:
            self.assertFalse(ok); self.assertIn("budget", why)

    def test_search_refuses_without_contact(self):
        ad = epa.EbayPoliteAdapter(store=self.store)
        sh.set_state("ebay", "BLOCKED", "t", self.store)
        with self.assertRaises(epa.SourceUnavailable):
            ad.search("shohei ohtani card")          # raises before any browser/network use

    def test_helpers(self):
        self.assertEqual(epa._grade("2023 Prizm Wembanyama PSA 10 Gem"), "PSA 10")
        self.assertEqual(epa._grade("Kobe BGS 9.5 refractor"), "BGS 9.5")
        self.assertEqual(epa._grade("raw card"), "")
        self.assertEqual(epa._price("$1,234.56"), 1234.56)

    def test_stage_writes_rows_and_meta(self):
        ad = epa.EbayPoliteAdapter(store=self.store)
        p = ad.stage([{"title": "x", "url": "https://www.ebay.com/itm/1"}], "freshness", "q", "2026-08-18")
        self.assertTrue(os.path.exists(p)); self.assertTrue(os.path.exists(p + ".meta.json"))
        self.assertIn("external_store/staging/ebay", p)


if __name__ == "__main__":
    unittest.main()
