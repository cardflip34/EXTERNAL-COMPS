#!/bin/bash
# Smoke test for comp_image_server.py's two doors (2026-10-01). Local only: a temp ROOT with a few fake images,
# a tiny allow-list built by tools/build_image_allowlist.py --from-urls, the REAL server started as a subprocess
# on random free ports, curl for allowed/blocked paths, a live reload, then the server is stopped.
#
#   bash external_engine/tests/smoke_image_server.sh            # internal door on loopback (allowed: 127.0.0.1/32)
#   SMOKE_TAILNET=1 bash external_engine/tests/smoke_image_server.sh
#                                                               # internal door on THIS machine's tailnet address
#                                                               # (the production default 'tailnet:PORT'), temp data only
# Never uses ports 55432/8011/8013/8510. Exit 0 = every check passed.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY=/usr/bin/python3
T="$(mktemp -d -t imgsmoke)"
PID=""
cleanup() { [ -n "$PID" ] && kill "$PID" 2>/dev/null && wait "$PID" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT

free_port() { "$PY" - <<'P'
import socket
bad = {55432, 8011, 8013, 8510}
while True:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    if p not in bad:
        print(p); break
P
}
PUB=$(free_port); INT=$(free_port)
HOST=stavross-mac-mini.tail9fccf8.ts.net

mk() { d="$T/root/$1/$2"; [ "${4:-flat}" = sharded ] && d="$T/root/$1/$(echo "$2" | cut -c1-2)/$(echo "$2" | cut -c3-4)/$2"
       mkdir -p "$d"; head -c 4000 /dev/urandom > "$d/01.$3"; }
mk scp_catalog aaaa1111bbbb2222cccc jpg sharded       # on the list
mk scp_catalog dddd3333eeee4444ffff jpg               # NOT on the list (exists)
mk ebay 206000000001 webp sharded                     # on the list (a VeeFriends picture)
mk ebay 206000000002 jpg sharded                      # NOT on the list (bulk archive)
mk fanatics fan123456 jpg sharded                     # bulk archive
mk tcgplayer_catalog tcg0001 jpg
mk lotphoto_goldin 202512-1012-5440-46c13855 jpg      # curated: public as a whole folder
mkdir -p "$T/lists" "$T/cache"
cat > "$T/urls.txt" <<EOF
https://$HOST/img/scp_catalog/aaaa1111bbbb2222cccc
https://$HOST/img/ebay/206000000001
https://other.example/img/ebay/206000000002
EOF
"$PY" "$REPO/tools/build_image_allowlist.py" --from-urls "$T/urls.txt" --out "$T/lists/public_allowlist.txt" \
      --min-keys 1 >"$T/build.json" 2>"$T/build.err" || { echo "FAIL builder"; cat "$T/build.err"; exit 1; }

if [ "${SMOKE_TAILNET:-0}" = 1 ]; then
  IBIND="tailnet:$INT"; IALLOW="100.64.0.0/10,fd7a:115c:a1e0::/48"
  TIP="$("$PY" -c "import sys; sys.path.insert(0,'$REPO/external_engine'); import comp_image_server as c; a=[x for x in c.tailnet_addresses() if ':' not in x]; print(a[0] if a else '')")"
  [ -n "$TIP" ] || { echo "FAIL no tailnet IPv4 on this machine"; exit 1; }
  IURL="http://$TIP:$INT"
else
  IBIND="127.0.0.1:$INT"; IALLOW="127.0.0.1/32"; IURL="http://127.0.0.1:$INT"
fi

MAZI_COMP_IMAGES_ROOT="$T/root" MAZI_IMAGE_CACHE_DIR="$T/cache" MAZI_IMAGE_ALLOWLIST_POLL_S=1 MAZI_CARD_SLUG_MAP="$T/none.csv" \
  "$PY" -u "$REPO/external_engine/comp_image_server.py" --public-bind "127.0.0.1:$PUB" --internal-bind "$IBIND" \
  --internal-allow "$IALLOW" --allowlist "$T/lists/public_allowlist.txt" --staged "$T/lists/staged.txt" \
  --access-log "$T/access.log" >"$T/server.err" 2>&1 &
PID=$!
for _ in $(seq 1 50); do curl -s -o /dev/null "http://127.0.0.1:$PUB/healthz" && grep -q "INTERNAL door" "$T/server.err" && break; sleep 0.2; done

FAILS=0
check() { # check NAME EXPECTED_CODE URL [curl args...]
  local name=$1 want=$2 url=$3; shift 3
  local got; got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" "$url")
  if [ "$got" = "$want" ]; then echo "ok   $want  $name"; else echo "FAIL $name: want $want got $got ($url)"; FAILS=$((FAILS+1)); fi
}
P="http://127.0.0.1:$PUB"
echo "-- public door 127.0.0.1:$PUB (what Serve/Funnel proxies to)"
check "listed scp_catalog key"                 200 "$P/img/scp_catalog/aaaa1111bbbb2222cccc"
check "listed VeeFriends ebay key"             200 "$P/img/ebay/206000000001"
check "listed key, HEAD"                       200 "$P/img/ebay/206000000001" -I
check "curated lot photo (not listed)"         200 "$P/img/lotphoto_goldin/202512-1012-5440-46c13855"
check "unlisted ebay id (exists on disk)"      404 "$P/img/ebay/206000000002"
check "unlisted ebay id, spoofed TS headers"   404 "$P/img/ebay/206000000002" -H "Tailscale-User-Login: andy@example.com" -H "X-Forwarded-For: 100.100.1.1"
check "unlisted scp key (exists on disk)"      404 "$P/img/scp_catalog/dddd3333eeee4444ffff"
check "fanatics archive"                       404 "$P/img/fanatics/fan123456"
check "tcgplayer archive"                      404 "$P/img/tcgplayer_catalog/tcg0001"
check "/card route"                            404 "$P/card/baseball-cards-2020-topps/mike-trout-1"
check "/r route"                               404 "$P/r?u=https%3A//storage.googleapis.com/images.pricecharting.com/a/240.jpg"
check "usage page"                             404 "$P/"
check "traversal"                              404 "$P/img/ebay/.." --path-as-is
check "traversal 2"                            404 "$P/img/../../etc/passwd" --path-as-is
check "/.env probe"                            404 "$P/.env"
check "POST"                                   405 "$P/img/ebay/206000000001" -X POST
check "healthz"                                200 "$P/healthz"
HZ=$(curl -s "$P/healthz"); [ "$HZ" = '{"ok": true}' ] && echo "ok   healthz body is $HZ" || { echo "FAIL healthz body: $HZ"; FAILS=$((FAILS+1)); }
A=$(curl -s -D - -o /dev/null "$P/img/ebay/206000000002" | tr -d '\r' | grep -viE '^(date):'); B=$(curl -s -D - -o /dev/null "$P/img/ebay/999999999999" | tr -d '\r' | grep -viE '^(date):')
[ "$A" = "$B" ] && echo "ok   denied-existing and truly-missing 404s are byte-identical (headers)" || { echo "FAIL 404s differ"; FAILS=$((FAILS+1)); }
H=$(curl -s -D - -o /dev/null "$P/img/scp_catalog/aaaa1111bbbb2222cccc" | tr -d '\r')
echo "$H" | grep -qi '^access-control-allow-origin: \*' && echo "$H" | grep -qi '^cache-control: public, max-age=31536000, immutable' \
  && echo "ok   allowed image keeps CORS * and immutable caching" || { echo "FAIL CORS/caching headers"; FAILS=$((FAILS+1)); }

echo "-- internal door $IBIND (full archive; peers $IALLOW)"
check "unlisted ebay id"                       200 "$IURL/img/ebay/206000000002"
check "fanatics archive"                       200 "$IURL/img/fanatics/fan123456"
check "unlisted scp key"                       200 "$IURL/img/scp_catalog/dddd3333eeee4444ffff"
check "usage page"                             200 "$IURL/"
check "full healthz"                           200 "$IURL/healthz"
curl -s "$IURL/healthz" | "$PY" -c "import json,sys; d=json.load(sys.stdin); a=d['public']['allowlist']; print('ok   internal healthz: allowlist loaded=%s total=%s counts=%s' % (a['loaded'], a['total'], a['counts']))"

echo "-- live reload (poll 1 s): rebuild the list with the other ebay id"
printf '%s\n' "https://$HOST/img/scp_catalog/aaaa1111bbbb2222cccc" "https://$HOST/img/ebay/206000000002" > "$T/urls2.txt"
"$PY" "$REPO/tools/build_image_allowlist.py" --from-urls "$T/urls2.txt" --out "$T/lists/public_allowlist.txt" --min-keys 1 --allow-shrink >/dev/null 2>&1
sleep 2.5
check "newly listed ebay id"                   200 "$P/img/ebay/206000000002"
check "de-listed ebay id"                      404 "$P/img/ebay/206000000001"
echo "-- bad list file: last good stays"
printf '# mazi-image-allowlist v1\nebay/206000000001\n' > "$T/lists/public_allowlist.txt"     # no trailer = truncated
sleep 2.5
check "still listed after bad file"            200 "$P/img/ebay/206000000002"
check "still de-listed after bad file"         404 "$P/img/ebay/206000000001"
echo "-- staged key"
echo "https://$HOST/img/ebay/206000000001" > "$T/lists/staged.txt"; sleep 2.5
check "staged ebay id"                         200 "$P/img/ebay/206000000001"

echo "-- server log (stderr)"; sed -E 's/^/   /; s/100\.([0-9]+)\.[0-9]+\.[0-9]+/100.\1.x.x/g' "$T/server.err"
echo "-- access log sample"; tail -n 4 "$T/access.log" | sed 's/^/   /'
grep -qE '127\.0\.0\.1|100\.[0-9]+\.[0-9]+\.[0-9]+|andy@example' "$T/access.log" && { echo "FAIL access log contains an address or header value"; FAILS=$((FAILS+1)); } || echo "ok   access log has no addresses or header values"
kill "$PID"; wait "$PID" 2>/dev/null; PID=""
echo "server stopped; $FAILS failure(s)"
exit $([ "$FAILS" = 0 ] && echo 0 || echo 1)
