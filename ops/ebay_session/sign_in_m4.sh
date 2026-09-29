#!/bin/bash
# sign_in.sh (M4, 2nd eBay account "Jack Locker" ja_667044) -- done by Andy (never by Claude: credentials are yours to type).
# Opens a SEPARATE Chrome with its OWN profile (it cannot touch the Whatnot bot Chromes or your everyday Chrome), with a
# debugging port that listens on 127.0.0.1 ONLY. An ssh tunnel lets the sweep on the Mini drive ONE tab in this window.
#   1. Sign in with ja_667044, finish 2FA, tick "Stay signed in".
#   2. Search "topps", tick Sold Items, confirm sold prices show.
#   3. LEAVE THIS WINDOW OPEN. The sweep opens its own tab in it and closes only that tab.
PROFILE=$HOME/mazi_ebay_session_m4/chrome_profile
mkdir -p "$PROFILE"; chmod 700 "$PROFILE"
open -na "Google Chrome" --args --user-data-dir="$PROFILE" --remote-debugging-port=9334 --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check "https://signin.ebay.com/"
