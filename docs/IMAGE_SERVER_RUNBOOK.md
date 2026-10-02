# Comp image server: public allow-list runbook

Owner: EXTERNAL COMPS lane. Server: `external_engine/comp_image_server.py`, run on the Mac mini by
`external_engine/supervisor.py` from `~/whatnot-sniper` (NOT from `~/EXTERNAL-COMPS`). Mini home: `/Users/stavrosaimini`.
Written 2026-10-02 for Andy's request "lock down the mini image server" (2026-10-01).

## What runs where

| Piece | Where | Notes |
|---|---|---|
| Public door | `127.0.0.1:8510` | Tailscale Serve/Funnel target (unchanged). Serves only allow-listed `/img/<source>/<key>`, the two lot-photo folders, and `/healthz` -> `{"ok": true}`. Everything else gets one identical 404. |
| Internal door | Mini tailnet IPs, port `8512` | Full archive, `/card`, `/r`, full `/healthz`. Only for OTHER tailnet devices (M4, Andy's MacBook Pro): `http://stavross-mac-mini.tail9fccf8.ts.net:8512/...`. Connections from the Mini's own addresses are refused, so a Serve or Funnel pointed at 8512 by mistake can't publish the archive. Tools on the Mini read the files directly. |
| Allow-list | `~/mazi_local_evidence/image_allowlist/public_allowlist.txt` | Built by `tools/build_image_allowlist.py` from `beta_catalog.image_small/_large`. The server re-reads it within about 30 s and ignores a file that does not parse. |
| Last-good copy | `.../public_allowlist.last_good.txt` | Written by the server after every successful load. At a restart, if the main file is missing or bad, the server loads this copy and logs `PUBLIC ALLOW-LIST FALLBACK`. |
| Staged keys | `.../staged.txt` (+ `staged.last_good.txt`) | Hand-edited, `<source>/<key>` or full `/img/` URLs. Public at once (within 30 s). **Never expires on its own.** |
| Mode | `.../mode` | `enforce` or `observe`, polled, no restart. No file means enforce. Anything else in the file means enforce, with a loud log line. |
| Builder schedule | `~/Library/LaunchAgents/com.mazi.image-allowlist.plist` | 04:40 and 16:40, with `--prune-staged`. Log: `~/Library/Logs/mazi_external_comps/image_allowlist.out`. |
| Access log | `~/Library/Logs/mazi_external_comps/comp_image_access.log` | `pub|int`, mode, decision, verb, status, bytes, path, peer class, header-presence flags, user-agent family, referer host. Never addresses or header values. |

Lot photos (`lotphoto_goldin`, `lotphoto_fanatics`, ~198 files) are public as whole folders so
`apply-catalog-art.py` can check the served sha256 before the beta row exists. They are never read from or copied
to the SSD cache, and they are sent with `Cache-Control: public, max-age=3600` (not "immutable"). Deleting the 6TB
file therefore takes one down at once on our side.

## Takedown

A picture is public while ANY of these hold: its key is on the allow-list (the beta references it), its key is in
`staged.txt`, or it is a lot photo whose 6TB file exists. A takedown removes all three.

1. **Remove it from the beta** (the row's `image_small` / `image_large`) through the normal MAZIDEX data path: dry
   run, canary, then full, with a backup. The beta is shared with the live site.
2. **Rebuild the list now** rather than waiting up to 12 h:
   `launchctl kickstart gui/$(id -u)/com.mazi.image-allowlist` (or run the builder by hand). If the takedown
   removes more than 10% of the total or of one source, the builder refuses (exit 3) until it is re-run with
   `--allow-shrink`.
3. **Remove the key from `staged.txt`** if it is there: `grep -n '<key>' ~/mazi_local_evidence/image_allowlist/staged.txt`.
   Every build reports staged keys the beta does not reference (`not_referenced_keys` in its JSON output); a
   withdrawn key that is still staged shows up there.
4. **Lot photo:** delete its folder on the 6TB, e.g.
   `rm -r /Volumes/MAZI_EVIDENCE_6TB/comp_images/lotphoto_goldin/<key>` (the takedown itself; a 24-hour
   obligation for house requests). There is no SSD copy to delete: the server never caches lot photos, and it
   purges copies left by older servers on start and every 30 min. To double-check:
   `find ~/.cache/mazi_comp_images/lotphoto_goldin ~/.cache/mazi_comp_images/lotphoto_fanatics -type f | wc -l` -> 0.
5. **Verify:** `curl -s -o /dev/null -w '%{http_code}\n' https://stavross-mac-mini.tail9fccf8.ts.net/img/<source>/<key>` -> 404,
   and `grep ' deny:unlisted .*/img/<source>/<key>' ~/Library/Logs/mazi_external_comps/comp_image_access.log | tail -1`.

What the server cannot recall: copies already in browsers and CDNs. Lot photos carry a 1 h max-age from this version
on. Anything served before 2026-10-02, and every catalog picture, carried `max-age=31536000, immutable`.

Emergency, everything at once: `tailscale funnel reset` takes the public door off the internet entirely. That is a
Tailscale config change (Tier 3, operator only), and it blanks every picture on the site.

## Staging keys before a beta write (VeeFriends apply and similar)

`apply_veefriends_images.py` HEADs the public URL before it writes the beta row. A new key is 404 publicly until it is
listed, so stage it first:

    python3 -c "import csv; [print(r['served_url']) for r in csv.DictReader(open('picks.csv'))]" \
      | ssh mini 'cat >> ~/mazi_local_evidence/image_allowlist/staged.txt'
    ssh mini 'tail -3 ~/Library/Logs/mazi_external_comps/comp_image_server.out'    # "staged allow-list loaded: N keys"

Any lane that writes new picture URLs into `beta_catalog` should stage the keys first, or kickstart the builder
right after the write. Otherwise the new tiles 404 for up to 12 h. The scheduled build drops staged keys once they are
listed (`--prune-staged`), so a later withdrawal from the beta also takes them down. Keys that were staged but never
applied stay public until someone removes them, and every build lists them.

## Observe / enforce

    echo observe > ~/mazi_local_evidence/image_allowlist/mode   # serve everything publicly, log would_deny:<reason>
    echo enforce > ~/mazi_local_evidence/image_allowlist/mode   # the allow-list decides
    grep 'PUBLIC door mode' ~/Library/Logs/mazi_external_comps/comp_image_server.out | tail -1   # within ~30 s

Watch for site traffic that the list would miss (observe) or does miss (enforce):

    LOG=~/Library/Logs/mazi_external_comps/comp_image_access.log
    UA='ua=(browser|googlebot|bingbot|applebot|vercel|node|facebook|twitter|slack|discord|whatsapp|telegram|linkedin|otherbot)'
    grep -E ' pub (observe would_deny|enforce deny):(unlisted|no_list_closed) ' $LOG | grep -E "$UA" \
      | grep -oE '/img/[a-z_]+/[^ ]+ .*ref=[^ ]+' | sort | uniq -c | sort -rn | head -20

`no_list_closed` means no list loaded at all, neither the main file nor the last-good copy. Fix that first.
`deny:route` from `ua=other`/`curl` is scanner traffic and expected.

## Deploy

Tier 2/3: needs Andy's go. Tell the MAZIDEX and VeeFriends lanes before the restart (step 6). Do not run the smoke
test on the Mini: it refuses to run there, because the supervisor's `pgrep -f external_engine/comp_image_server.py`
must only ever find the real lane. The unit tests are safe there.

**Do not check out, pull or merge in `~/EXTERNAL-COMPS`.** Other lanes use its working tree (on 2026-10-01 it was on
`feat/headline-new-houses`). Take the reviewed files by commit instead:

0. Preflight (read-only):

        git -C ~/EXTERNAL-COMPS status -sb | head -2                     # note it; leave it alone
        md5 -q ~/whatnot-sniper/external_engine/comp_image_server.py      # bed0f49e1dbaa881dbf6e7bef67e3c28 else STOP
        pgrep -f external_engine/comp_image_server.py | wc -l             # 1 else STOP

1. Fetch the merged commit `M` (refs and objects only: no change to the checked-out branch or files) and check the
   blobs are the reviewed ones:

        M=<merge sha on origin/main>
        git -C ~/EXTERNAL-COMPS fetch origin main
        git -C ~/EXTERNAL-COMPS merge-base --is-ancestor $M origin/main && echo on-main
        F="external_engine/comp_image_server.py tools/build_image_allowlist.py external_engine/tests/test_image_server_allowlist.py external_engine/tests/smoke_image_server.sh ops/launchd/com.mazi.image-allowlist.plist"
        for f in $F; do echo "$(git -C ~/EXTERNAL-COMPS rev-parse $M:$f)  $f"; done
        # each id must equal the reviewed blob id in the PR / lane reply; any difference = STOP

2. Unpack into a staging folder and run the unit tests there:

        STAGE=~/mazi_local_evidence/image_server_deploy_$(date +%Y%m%d)
        mkdir -p $STAGE && git -C ~/EXTERNAL-COMPS archive $M $F | tar -x -C $STAGE
        cd $STAGE && /usr/bin/python3 -m unittest external_engine/tests/test_image_server_allowlist.py   # OK

3. Back up, install into `~/whatnot-sniper`, and check by content hash. The supervisor runs that tree.
   `sync_from_project.sh` mirrors its `external_engine/` into the repo with `--delete`, so the files must live there:

        B=~/mazi_local_evidence/image_server_backup_$(date +%Y%m%d)
        mkdir -p $B && cp -p ~/whatnot-sniper/external_engine/comp_image_server.py $B/
        for f in $F; do mkdir -p ~/whatnot-sniper/$(dirname $f) && cp $STAGE/$f ~/whatnot-sniper/$f; done
        for f in $F; do [ "$(git hash-object ~/whatnot-sniper/$f)" = "$(git -C ~/EXTERNAL-COMPS rev-parse $M:$f)" ] \
          && echo "ok $f" || echo "MISMATCH $f"; done                    # all ok; else restore $B and STOP

   The running server keeps the old code until step 6. Tell the headline lane: their branch and working tree are
   untouched. But `sync_from_project.sh` run on their branch will now copy the new server and two test files into
   their working tree. They should not commit those on their branch; rebase on main first.

4. Observe mode first, set BEFORE the restart:

        mkdir -p ~/mazi_local_evidence/image_allowlist && echo observe > ~/mazi_local_evidence/image_allowlist/mode

5. Build the list once. Run it off-peak: this is the first full read of the beta and the only measurement of how long
   one takes. No dry run is needed, because nothing reads the file before the restart:

        cd ~/whatnot-sniper && /usr/bin/python3 tools/build_image_allowlist.py 2>&1 | tail -40
        /usr/bin/python3 tools/build_image_allowlist.py --check
        # expect ~398K keys: scp_catalog <= 393,160, ebay 5,036, lotphoto ~56; url_outcomes.our_host_other_route = 0;
        # note "beta scan done ... Ns". Exit 3 = a guard refused (read "refused"); 4 = database error / --max-seconds.

   Then install the schedule. The first scheduled run proves psycopg and the password file work under launchd:

        cp ~/whatnot-sniper/ops/launchd/com.mazi.image-allowlist.plist ~/Library/LaunchAgents/
        plutil -lint ~/Library/LaunchAgents/com.mazi.image-allowlist.plist
        launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mazi.image-allowlist.plist
        # after the next 04:40/16:40: tail ~/Library/Logs/mazi_external_comps/image_allowlist.out -> "wrote N keys"

6. Restart into the new code (still observe). The supervisor relaunches the server within one 60 s tick:

        P=$(pgrep -f external_engine/comp_image_server.py); [ $(echo $P | wc -w) -eq 1 ] && ps -o pid,etime,command -p $P
        kill $P; sleep 75
        lsof -nP -a -p $(pgrep -f external_engine/comp_image_server.py) -iTCP -sTCP:LISTEN
        # 127.0.0.1:8510, <tailnet v4>:8512, [fd7a:...]:8512 -- and no *:8510
        tail -30 ~/Library/Logs/mazi_external_comps/comp_image_server.out
        # "PUBLIC door mode: observe", "public allow-list loaded: N keys", "PUBLIC door (observe, file) on
        # http://127.0.0.1:8510", "INTERNAL door (full archive) on ...", and once: "ssd cache: removed ~57 cached
        # copies of lotphoto_goldin/lotphoto_fanatics" (SSD copies only; the 6TB originals are not touched)

7. Observe for 24-48 h with the watch command above. The gate: zero `would_deny` on site, OG and crawler traffic.
   Expected and harmless: internal VeeFriends jobs (`ua=python`), which move to `:8512` or staging, and scanner
   junk. If observe is skipped, write down why in the lane reply.

8. Enforce (no restart): `echo enforce > ~/mazi_local_evidence/image_allowlist/mode`, then check the mode log line.
   Verify from outside by pinning the public Funnel address (MagicDNS would route M4 over the tailnet):

        H=stavross-mac-mini.tail9fccf8.ts.net; IP=$(dig +short $H @1.1.1.1 | grep -E '^[0-9.]+$' | head -1)
        K=$(ssh mini "grep -m1 '^scp_catalog/' ~/mazi_local_evidence/image_allowlist/public_allowlist.txt")
        curl -s -o /dev/null -w '%{http_code}\n' --resolve $H:443:$IP https://$H/img/$K                     # 200
        curl -s -o /dev/null -w '%{http_code}\n' --resolve $H:443:$IP https://$H/img/ebay/206162825097     # 404 (was 200)
        curl -s -o /dev/null -w '%{http_code}\n' --resolve $H:443:$IP https://$H/card/basketball-cards-1996-topps-chrome/kobe-bryant-138  # 404
        curl -s --resolve $H:443:$IP https://$H/healthz                                                    # {"ok": true}
        # from M4 (another tailnet device): http://$H:8512/img/ebay/206162825097 -> 200
        # on the Mini itself: curl http://<own tailnet ip>:8512/healthz -> no reply (own address refused, by design)

   For the first 24 h, watch `enforce deny:(unlisted|no_list_closed)` with the command above.

## Rollback

Full rollback to the old open server (about 2 min). Every step is mandatory: the installed builder imports the
server module and crashes against the old one, and `sync_from_project.sh` would put the new files back into the
repo.

    B=~/mazi_local_evidence/image_server_backup_<deploy date YYYYMMDD>        # from deploy step 3
    launchctl bootout gui/$(id -u)/com.mazi.image-allowlist 2>/dev/null; rm -f ~/Library/LaunchAgents/com.mazi.image-allowlist.plist
    cp $B/comp_image_server.py ~/whatnot-sniper/external_engine/comp_image_server.py
    md5 -q ~/whatnot-sniper/external_engine/comp_image_server.py         # bed0f49e1dbaa881dbf6e7bef67e3c28
    rm -f ~/whatnot-sniper/tools/build_image_allowlist.py ~/whatnot-sniper/ops/launchd/com.mazi.image-allowlist.plist \
          ~/whatnot-sniper/external_engine/tests/test_image_server_allowlist.py ~/whatnot-sniper/external_engine/tests/smoke_image_server.sh
    P=$(pgrep -f external_engine/comp_image_server.py); [ $(echo $P | wc -w) -eq 1 ] && kill $P; sleep 75
    lsof -nP -a -p $(pgrep -f external_engine/comp_image_server.py) -iTCP -sTCP:LISTEN     # *:8510 again
    curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8510/img/ebay/206162825097  # 200 (open again)

Then revert the merge on main (operator-approved push), and before anyone runs `sync_from_project.sh`, confirm that
`~/whatnot-sniper` matches the reverted main (`R` = the revert commit, fetched as in deploy step 1):

    git -C ~/EXTERNAL-COMPS fetch origin main
    [ "$(git hash-object ~/whatnot-sniper/external_engine/comp_image_server.py)" = "$(git -C ~/EXTERNAL-COMPS rev-parse $R:external_engine/comp_image_server.py)" ] && echo server-ok
    for f in tools/build_image_allowlist.py ops/launchd/com.mazi.image-allowlist.plist external_engine/tests/test_image_server_allowlist.py external_engine/tests/smoke_image_server.sh; do
      [ -e ~/whatnot-sniper/$f ] && echo "STILL THERE $f"; git -C ~/EXTERNAL-COMPS cat-file -e $R:$f 2>/dev/null && echo "STILL IN REPO $f"; done

The files in `~/mazi_local_evidence/image_allowlist/` and the access log do nothing under the old code.

Partial loosening without a rollback: `echo observe > ~/mazi_local_evidence/image_allowlist/mode` (public door
serves everything again within ~30 s, logs `would_deny`), or add keys to `staged.txt`.

## Known limits

- The builder reads the whole `beta_catalog` heap (~6.2 GB) each run. No index covers the picture columns. It
  uses one sequential scan, which goes through Postgres's bulk-read ring buffer and so leaves the site's cached pages
  in place, but it still costs disk reads on a shared Small instance. Hence two runs a day. A partial index
  `ON beta_catalog (card_id) WHERE image_small IS NOT NULL OR image_large IS NOT NULL` would cut a build to ~400K
  rows. That is a Tier-3 schema change and has not been made.
- Full-build time on the live beta has not been measured yet (deploy step 5 measures it). `--max-seconds 1200`
  bounds it.
- Tailscale identity headers are only logged as present or absent. Nothing relies on them.
- The internal door admits any tailnet address except the Mini's own. Narrowing it to exact peers needs
  `--internal-allow` in the supervisor's lane `cmd`.
