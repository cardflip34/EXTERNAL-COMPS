#!/usr/bin/env python3
"""Tests for tools/export_headline_beta.py selection (build): what may be written, what is held. No DB.

  python3 tools/test_export_headline_beta.py
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_headline_beta as E  # noqa: E402


def sale(price=250_000, venue="goldin", status="resolved", prec="day", grade="PSA 10", src="neon", sid="L1", card="mazi:bb:x:y:1"):
    return {"title": "1952 Topps #311 Mickey Mantle PSA 10", "price": price, "date": "2026-06-01", "prec": prec, "venue": venue,
            "grade": grade, "sources": [{"src": src, "venue": venue, "source_id": sid, "url": "https://goldin.co/item/x"}],
            "mazi": {"status": status, "candidates": [{"card_id": card}] if status.startswith("resolved") else []}}


class Build(unittest.TestCase):
    def test_resolved_goldin_sale_becomes_a_row(self):
        rows, held = E.build([sale()], set())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["sale_id"], r["card_id"], r["venue"], r["provider"], r["grade"], r["date_precision"]),
                         ("mazi-hl:goldin:L1", "mazi:bb:x:y:1", "goldin", "mazi_headline", "PSA 10", "day"))
        self.assertEqual(r["occurrence_key"], "goldin:L1:2026-06-01:250000.0")
        self.assertFalse(r["verified"]); self.assertTrue(r["identity_reviewed"] and r["price_eligible"] and r["published"])
        self.assertIn("buyer's premium", r["evidence_note"])

    def test_million_dollar_sales_need_andys_approval(self):
        big = sale(price=2_500_000, status="resolved_needs_review")
        rows, held = E.build([big], set())
        self.assertEqual((len(rows), held["$1M+ not yet approved by Andy"]), (0, 1))
        rows, _ = E.build([big], {"mazi-hl:goldin:L1"})
        self.assertEqual(len(rows), 1)

    def test_held_cases(self):
        rows, held = E.build([sale(prec="month"), sale(grade=None, sid="L2"), sale(src="seed", sid=None)], set())
        self.assertEqual(rows, [])
        self.assertEqual((held["month-precision date"], held["no grade read from the title"], held["no venue record (press only)"]),
                         (1, 1, 1))

    def test_black_label_is_held(self):
        s = sale(); s["title"] = "2003-04 Topps Chrome #111 LeBron James BGS 10 Pristine Black Label"; s["grade"] = "BGS 10"
        rows, held = E.build([s], set())
        self.assertEqual((rows, held["BGS Black Label (its own grade bucket is not decided yet)"]), ([], 1))

    def test_not_eligible_at_all(self):
        rows, held = E.build([sale(price=99_000), sale(status="needs_review"), sale(status="mint_candidate"),
                              sale(venue="unknown")], set())
        self.assertEqual((rows, sum(held.values())), ([], 0))


if __name__ == "__main__":
    unittest.main(verbosity=1)
