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


if __name__ == "__main__":
    unittest.main(verbosity=1)
