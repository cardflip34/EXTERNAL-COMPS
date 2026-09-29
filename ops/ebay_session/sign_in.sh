#!/bin/bash
# sign_in.sh -- done by Andy (never by Claude: credentials are yours to type).
# Opens a SEPARATE Chrome with its OWN profile (it cannot touch the Whatnot bot Chromes), with a debugging
# port that listens on 127.0.0.1 ONLY, so the VeeFriends pilot can attach to THIS window's live session.
#   1. Sign in with the DEDICATED eBay account, finish 2FA, tick "Stay signed in".
#   2. Search "veefriends", tick Sold Items, confirm sold prices show.
#   3. LEAVE THIS WINDOW OPEN. The pilot opens its own tab in it and closes only that tab.
PROFILE=$HOME/mazi_ebay_session/chrome_profile
mkdir -p "$PROFILE"; chmod 700 "$PROFILE"
open -na "Google Chrome" --args --user-data-dir="$PROFILE" --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 \
  --no-first-run --no-default-browser-check "https://signin.ebay.com/"
