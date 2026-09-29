#!/bin/bash
# Runs the VeeFriends eBay pilot inside the Mini's desktop session (keychain + window access).
cd ~/whatnot-sniper && /usr/bin/python3 tools/ebay_veefriends_pilot.py --operator-approved --floor 2026-07-01 --max-pages 2 2>&1 \
  | tee ~/mazi_veefriends/ebay_pilot/last_run.log
