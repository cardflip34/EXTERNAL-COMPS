#!/usr/bin/env python3
"""manage_lanes() must honour each lane's max_level when stopping on RED (2026-10-03). No processes are touched.

  python3 external_engine/tests/test_supervisor_lanes.py
"""
import os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import supervisor as S  # noqa: E402


class RedStops(unittest.TestCase):
    def setUp(self):
        self.killed, self.started = [], []
        self._pids, self._kill, self._log = S._lane_pids, S.os.kill, S._log
        names = {l["pattern"]: 100 + i for i, l in enumerate(S.MANAGED_LANES)}
        S._lane_pids = lambda pattern: [names[pattern]]          # every lane is running
        S.os.kill = lambda pid, sig: self.killed.append(pid)
        S._log = lambda msg: None
        self.pid = {l["name"]: names[l["pattern"]] for l in S.MANAGED_LANES}

    def tearDown(self):
        S._lane_pids, S.os.kill, S._log = self._pids, self._kill, self._log

    def test_red_streak_stops_yellow_lanes_but_never_the_image_server(self):
        state = {}
        for _ in range(S.RED_STOP_TICKS):
            S.manage_lanes(state, "RED")
        self.assertIn(self.pid["sources_supervisor"], self.killed)
        self.assertNotIn(self.pid["comp_image_server"], self.killed)
        self.assertEqual(state.get("lanes", state)["comp_image_server"]["status"] if "lanes" in state else "running", "running")

    def test_short_red_stops_nothing(self):
        state = {}
        for _ in range(S.RED_STOP_TICKS - 1):
            S.manage_lanes(state, "RED")
        self.assertEqual(self.killed, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
