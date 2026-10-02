#!/usr/bin/env python3
"""Tests for tools/beta_catalog_images.py's photo guard and undo scope (2026-10-02). No database, no network.

  python3 tools/test_beta_catalog_images.py
"""
import json, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import beta_catalog_images as B  # noqa: E402

P = B.SCP_PREFIX


class PhotoGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for key, sharded in (("aaaa000000000000good", True), ("bbbb00000000000flat1", False)):
            d = (os.path.join(self.root, "scp_catalog", key[:2], key[2:4], key) if sharded
                 else os.path.join(self.root, "scp_catalog", key))
            os.makedirs(d)
            open(os.path.join(d, "01.jpg"), "wb").write(b"jpg")

    def tearDown(self):
        self.tmp.cleanup()

    def test_on_disk_both_layouts(self):
        self.assertTrue(B.photo_on_disk("aaaa000000000000good", self.root))
        self.assertTrue(B.photo_on_disk("bbbb00000000000flat1", self.root))
        self.assertFalse(B.photo_on_disk("cccc0000000000absent", self.root))

    def test_ledger_last_status_wins_and_skip_is_not_dead(self):
        led = os.path.join(self.root, "ledger.jsonl")
        with open(led, "w") as f:
            for k, s, c in (("k1", "dead", 404), ("k1", "ok", 200),       # recovered -> not dead
                            ("k2", "ok", 200), ("k2", "dead", 404),       # died later -> dead
                            ("k3", "skip", 0)):                            # already on disk -> not dead
                f.write(json.dumps({"k": k, "s": s, "c": c}) + "\n")
            f.write("not json\n")
        self.assertEqual(B.ledger_dead(led), {"k2"})
        self.assertEqual(B.ledger_dead(os.path.join(self.root, "missing.jsonl")), set())

    def test_usable_rows_drops_dead_missing_and_foreign(self):
        m = os.path.join(self.root, "map.csv")
        with open(m, "w") as f:
            f.write("slug,url\n")
            f.write(f"s/good,{P}aaaa000000000000good\n")
            f.write(f"s/flat,{P}bbbb00000000000flat1\n")
            f.write(f"s/dead,{P}bbbb00000000000flat1x\n")
            f.write(f"s/gone,{P}cccc0000000000absent\n")
            f.write(f"s/lot,{B.OUR_PREFIX}img/lotphoto_goldin/123\n")
        on_disk = lambda k: B.photo_on_disk(k, self.root)
        rows, n = B.usable_rows(m, {"bbbb00000000000flat1x"}, on_disk)
        self.assertEqual(rows, [("s/good", P + "aaaa000000000000good"), ("s/flat", P + "bbbb00000000000flat1")])
        self.assertEqual(n, {"rows": 5, "kept": 2, "not_scp_catalog": 1, "ledger_dead": 1, "not_on_disk": 1})


class UndoScope(unittest.TestCase):
    def test_undo_prefix_is_scp_catalog_only(self):
        self.assertTrue(B.SCP_PREFIX.endswith("/img/scp_catalog/"))
        for other in ("img/lotphoto_goldin/1", "img/lotphoto_fanatics/2", "img/ebay/206162825097"):
            self.assertFalse((B.OUR_PREFIX + other).startswith(B.SCP_PREFIX), other)


if __name__ == "__main__":
    unittest.main(verbosity=1)
