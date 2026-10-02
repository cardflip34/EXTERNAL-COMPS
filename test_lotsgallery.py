#!/usr/bin/env python3
"""Tests for lotsgallery_scraper.py parsing (Memory Lane / Lelands gallery markup, real snippet 2026-10-01). No network.

  python3 test_lotsgallery.py
"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lotsgallery_scraper as G  # noqa: E402

PAGE = """<select name="ctl00$Auction" onchange="javascript:setTimeout('__doPostBack(\\'ctl00$Auction\\',\\'\\')', 0)" id="Auction" class="form-control">
<option value="-1">All Auctions</option><option selected="selected" value="163">Summer Rarities Auction 2026</option>
<option value="55">The Buried Treasure Card &amp; Ball Auction</option></select>
<div> Summer Rarities Auction 2026 Start: 9/10/2026 9:00 AM ET End: 9/26/2026 7:00 PM ET Prices Shown Include Buyer's Premium.</div>
<div class="col-lg-3 col-md-4 col-sm-6"> <div class="item"> <h5 class="boxed">1</h5> <div class="item-details clearfix"> <p class="description">
<a href="https://bid.memorylaneinc.com/bids/bidplace.aspx?itemid=94115">1955 Topps #123 Sandy Koufax Rookie PSA 9 MINT</a> </p>
<p> Bids: <strong>54</strong> <br> Opening Bid: <strong>$50,000</strong> <br> Status: <strong>Sold</strong> </p> </div>
<div class="item-price"> <a href="https://bid.memorylaneinc.com//bids/bidplace.aspx?itemid=94115">SOLD FOR $604,736</a> </div> </div> </div>
<div class="col-lg-3"> <div class="item"> <h5 class="boxed">2</h5> <p class="description">
<a href="https://bid.memorylaneinc.com/bids/bidplace.aspx?itemid=94116">Babe Ruth Single-Signed Baseball (PSA)</a> </p>
<p> Bids: <strong>12</strong> <br> Status: <strong>Sold</strong> </p> <div class="item-price"> <a href="#">SOLD FOR $134,579</a> </div> </div> </div>
<div class="col-lg-3"> <div class="item"> <h5 class="boxed">3</h5> <p class="description">
<a href="https://bid.memorylaneinc.com/bids/bidplace.aspx?itemid=94117">1952 Topps #1 Andy Pafko PSA 8</a> </p>
<p> Bids: <strong>0</strong> <br> Status: <strong>Unsold</strong> </p> </div> </div>"""


class Parse(unittest.TestCase):
    def test_lots(self):
        lots = G.parse_gallery(PAGE)
        self.assertEqual([l["itemid"] for l in lots], ["94115", "94116", "94117"])
        self.assertEqual((lots[0]["lot"], lots[0]["bids"], lots[0]["status"], lots[0]["sold_price"]), ("1", 54, "Sold", 604736.0))
        self.assertIsNone(lots[2]["sold_price"])

    def test_auction_end_and_list(self):
        self.assertEqual(G.auction_end(PAGE), "2026-09-26")
        self.assertEqual(G.auctions(PAGE), [("163", "Summer Rarities Auction 2026"), ("55", "The Buried Treasure Card & Ball Auction")])

    def test_single_card_filter_on_these_lots(self):
        import rea_scraper as R
        lots = G.parse_gallery(PAGE)
        self.assertEqual([R.is_single_card(l["title"]) for l in lots], [True, False, True])


class _NavPage:
    """content() raises Playwright's navigating error `fails` times, then returns the html."""
    def __init__(self, fails, msg="Page.content: Unable to retrieve content because the page is navigating and changing the content."):
        self.fails, self.msg, self.calls, self.waits = fails, msg, 0, 0

    def content(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError(self.msg)
        return "<html>ok</html>"

    def wait_for_load_state(self, *_a, **_k):
        self.waits += 1


class TitleEntities(unittest.TestCase):
    def test_titles_are_unescaped(self):
        page = ('<div class="item"><h5 class="boxed">12</h5><a href="/bids/bidplace.aspx?itemid=9">1997 SP Authentic Sign of '
                'the Times Stars &amp; Rookies #MJ Michael Jordan</a> Status: <strong>Sold</strong> SOLD FOR $1,000</div>')
        self.assertEqual(G.parse_gallery(page)[0]["title"], "1997 SP Authentic Sign of the Times Stars & Rookies #MJ Michael Jordan")


class PageHtmlRetry(unittest.TestCase):
    def test_retries_while_navigating(self):
        pg = _NavPage(fails=2)
        self.assertEqual(G.page_html(pg, pause=0), "<html>ok</html>")
        self.assertEqual((pg.calls, pg.waits), (3, 2))

    def test_gives_up_after_tries(self):
        with self.assertRaises(RuntimeError):
            G.page_html(_NavPage(fails=9), tries=3, pause=0)

    def test_other_errors_raise_at_once(self):
        pg = _NavPage(fails=1, msg="Target page, context or browser has been closed")
        with self.assertRaises(RuntimeError):
            G.page_html(pg, pause=0)
        self.assertEqual(pg.calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
