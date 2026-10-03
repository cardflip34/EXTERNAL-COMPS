#!/usr/bin/env python3
"""Tests for bridge_local_sources_to_neon.txn_guard (2026-10-02). No database.

  python3 test_bridge_local_guard.py
"""
import os, re, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge_local_sources_to_neon as BR  # noqa: E402


class Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)


class TxnGuard(unittest.TestCase):
    def test_sets_local_timeout(self):
        c = Cur()
        BR.txn_guard(c)
        self.assertEqual(c.sql, ["SET LOCAL statement_timeout = '15min'"])

    def test_every_commit_is_followed_by_a_guard(self):
        src = open(BR.__file__).read()
        main_and_source = src[src.index("def bridge_source"):]
        for m in re.finditer(r"\\.commit\\(\\)", main_and_source):
            nxt = main_and_source[m.end():m.end() + 400]
            # a commit either ends the run (conn.close / verify follows) or is followed by txn_guard / bridge_source
            # (which guards its own first transaction)
            self.assertTrue("txn_guard(" in nxt or "bridge_source(" in nxt or "conn.close()" in nxt or "return" in nxt.split("\\n")[1],
                            main_and_source[m.start() - 120:m.end() + 120])


if __name__ == "__main__":
    unittest.main(verbosity=1)
