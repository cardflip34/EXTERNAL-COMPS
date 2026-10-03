#!/usr/bin/env python3
"""Browser() must stop Playwright when connect_over_cdp fails (2026-10-03). No browser, no network.

  python3 tools/test_ebay_sweep_browser.py
"""
import os, sys, types, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class FakePW:
    def __init__(self, fail):
        self.fail, self.stopped = fail, 0
        self.chromium = types.SimpleNamespace(connect_over_cdp=self.connect)

    def connect(self, cdp):
        if self.fail:
            raise TimeoutError("BrowserType.connect_over_cdp: Timeout 180000ms exceeded.")
        page = types.SimpleNamespace(close=lambda: None)
        return types.SimpleNamespace(contexts=[types.SimpleNamespace(new_page=lambda: page)])

    def stop(self):
        self.stopped += 1


class BrowserConnect(unittest.TestCase):
    def setUp(self):
        self.saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.sync_api", "ebay_bulk_scraper",
                                                      "ebay_player_scraper", "ebay_polite_adapter")}
        self.pw = None
        test = self

        class Starter:
            def start(self_inner):
                return test.pw
        sys.modules["playwright"] = types.ModuleType("playwright")
        sys.modules["playwright.sync_api"] = types.SimpleNamespace(sync_playwright=lambda: Starter())
        sys.modules["ebay_bulk_scraper"] = types.SimpleNamespace(extract_listings=None, block_signature=None)
        sys.modules["ebay_player_scraper"] = types.SimpleNamespace(parse_listing_sold_date=None)
        sys.modules["ebay_polite_adapter"] = types.SimpleNamespace(_price=None)
        import ebay_signed_in_sweep as S
        self.S = S
        self.args = types.SimpleNamespace(cdp="http://127.0.0.1:9333", break_every_min=50, break_every_max=90)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_failed_connect_stops_playwright_and_reraises(self):
        self.pw = FakePW(fail=True)
        with self.assertRaises(TimeoutError):
            self.S.Browser(None, self.args)
        self.assertEqual(self.pw.stopped, 1)

    def test_good_connect_keeps_playwright_running(self):
        self.pw = FakePW(fail=False)
        b = self.S.Browser(None, self.args)
        self.assertEqual(self.pw.stopped, 0)
        b.close()
        self.assertEqual(self.pw.stopped, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
