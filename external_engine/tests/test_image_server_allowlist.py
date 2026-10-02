#!/usr/bin/env python3
"""comp_image_server public/internal doors + the allow-list builder -- NO NETWORK (loopback sockets only).

    /usr/bin/python3 -m unittest external_engine/tests/test_image_server_allowlist.py -v

Covers: public vs internal decisions; 404 for unlisted keys and for the bulk sources; allowed keys served with
CORS + immutable caching; one identical 404 for every public refusal; reload; last-good on a bad/truncated/
missing file; fail-closed when no list ever loaded (scp + lot photos stay up); staged keys; path traversal and
symlink escape; spoofed Tailscale/forwarding headers grant nothing; internal door refuses peers outside its
CIDRs; the SSD cache never bypasses the allow-list; denied keys never touch the disk; access log carries header
presence but never addresses or header values; builder classify/collect/build/guards/keyset paging.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.dirname(HERE)
REPO = os.path.dirname(ENGINE)

_TMP = tempfile.mkdtemp(prefix="imgsrv_test_")
os.environ["MAZI_COMP_IMAGES_ROOT"] = os.path.join(_TMP, "root")
os.environ["MAZI_IMAGE_CACHE_DIR"] = os.path.join(_TMP, "cache")
sys.path.insert(0, ENGINE)
sys.path.insert(0, os.path.join(REPO, "tools"))

import comp_image_server as cis  # noqa: E402
import build_image_allowlist as bia  # noqa: E402

FORBIDDEN_PORTS = {55432, 8011, 8013, 8510}
ROOT = cis.ROOT
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 3000

SCP_LISTED = "aaaa1111bbbb2222cccc"
SCP_UNLISTED = "dddd3333eeee4444ffff"
EBAY_LISTED = "206000000001"
EBAY_UNLISTED = "206000000002"
EBAY_STAGED = "206000000003"
FAN = "fan123456"
TCG = "tcg0001"
LOT_G = "202512-1012-5440-46c13855"
LOT_F = "lotf-0001"
HOST = bia.IMAGE_HOST


def put(source, key, data=JPEG, ext=".jpg", sharded=True):
    k = key.lower()
    d = os.path.join(ROOT, source, k[:2], k[2:4], key) if sharded else os.path.join(ROOT, source, key)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "01" + ext), "wb") as f:
        f.write(data)
    return d


def write_list(path, entries, meta=None):
    with open(path, "w") as f:
        f.write(cis.format_allowlist(entries, meta or {"built_at": "test"}))
    bump(path)


def bump(path):
    """Make sure the poller sees a change even within one mtime tick."""
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def start(door, allow, mode="enforce", nets=()):
    for _ in range(20):
        srv = cis.DoorServer(("127.0.0.1", 0), cis.Handler, door=door, allow=allow, public_mode=mode,
                             allowed_nets=nets)
        if srv.server_address[1] not in FORBIDDEN_PORTS:
            cis._serve_in_thread(srv, f"test-{door}")
            return srv
        srv.server_close()
    raise RuntimeError("no free port")


def req(srv, path, method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    try:
        c.request(method, path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders() if k.lower() != "date"}
        return r.status, body, hdrs
    finally:
        c.close()


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def wait_for(self, *needles, timeout=3.0):
        """The access line is written after the response body (in do_GET's finally), so a client can
        read the response a moment before the server thread logs it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            joined = "\n".join(self.lines)
            if all(n in joined for n in needles):
                return joined
            time.sleep(0.02)
        return "\n".join(self.lines)


class ServerDoors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        put("scp_catalog", SCP_LISTED)
        put("scp_catalog", SCP_UNLISTED, sharded=False)        # legacy flat layout still resolves
        put("ebay", EBAY_LISTED, ext=".webp", data=b"RIFF\x00\x00\x00\x00WEBP" + b"w" * 3000)
        put("ebay", EBAY_UNLISTED)
        put("ebay", EBAY_STAGED)
        put("fanatics", FAN)
        put("tcgplayer_catalog", TCG)
        put("lotphoto_goldin", LOT_G, sharded=False)
        put("lotphoto_fanatics", LOT_F, sharded=False)
        # a symlink inside a curated (whole-folder public) source pointing into the ebay archive
        os.symlink(os.path.join(ROOT, "ebay", EBAY_UNLISTED[:2], EBAY_UNLISTED[2:4], EBAY_UNLISTED),
                   os.path.join(ROOT, "lotphoto_goldin", "evil"))
        cls.listdir = os.path.join(_TMP, "lists")
        os.makedirs(cls.listdir, exist_ok=True)
        cls.list_path = os.path.join(cls.listdir, "public_allowlist.txt")
        cls.staged_path = os.path.join(cls.listdir, "staged.txt")
        write_list(cls.list_path, {"scp_catalog": {SCP_LISTED}, "ebay": {EBAY_LISTED},
                                   "lotphoto_goldin": {LOT_G}})
        cls.logs = []
        cls.allow = cis.AllowList(cls.list_path, cls.staged_path, log=cls.logs.append)
        cls.allow.maybe_reload()
        cls.nolist_logs = []
        cls.nolist = cis.AllowList(os.path.join(cls.listdir, "missing.txt"), None, log=cls.nolist_logs.append)
        cls.nolist.maybe_reload()
        cls.access = _ListHandler()
        cis._ACCESS.addHandler(cls.access)
        cls.pub = start("public", cls.allow)
        cls.pub_nolist = start("public", cls.nolist)
        cls.pub_observe = start("public", cls.allow, mode="observe")
        cls.internal = start("internal", cls.allow, nets=cis.parse_nets("127.0.0.0/8,::1/128"))
        cls.internal_strict = start("internal", cls.allow, nets=cis.parse_nets(cis.DEFAULT_INTERNAL_ALLOW))
        cis._SLUG_MAP.update(map={"baseball-cards-2020-topps/mike-trout-1": SCP_LISTED}, error=None)

    @classmethod
    def tearDownClass(cls):
        for s in (cls.pub, cls.pub_nolist, cls.pub_observe, cls.internal, cls.internal_strict):
            s.shutdown()
            s.server_close()
        cis._ACCESS.removeHandler(cls.access)
        shutil.rmtree(_TMP, ignore_errors=True)

    # ---------------------------------------------------------------- public: allowed
    def test_public_serves_listed_keys_with_cors_and_caching(self):
        for path, ctype in ((f"/img/scp_catalog/{SCP_LISTED}", "image/jpeg"), (f"/img/ebay/{EBAY_LISTED}", "image/webp")):
            st, body, h = req(self.pub, path)
            self.assertEqual(st, 200, path)
            self.assertEqual(h["content-type"], ctype)
            self.assertEqual(h["access-control-allow-origin"], "*")
            self.assertEqual(h["cache-control"], "public, max-age=31536000, immutable")
            self.assertEqual(int(h["content-length"]), len(body))
            self.assertGreater(len(body), 1000)

    def test_public_serves_whole_curated_lotphoto_sources(self):
        self.assertEqual(req(self.pub, f"/img/lotphoto_goldin/{LOT_G}")[0], 200)
        self.assertEqual(req(self.pub, f"/img/lotphoto_fanatics/{LOT_F}")[0], 200)   # not on the list

    def test_public_head_matches_get_without_body(self):
        st, body, h = req(self.pub, f"/img/scp_catalog/{SCP_LISTED}", method="HEAD")
        self.assertEqual((st, body), (200, b""))
        self.assertEqual(int(h["content-length"]), len(JPEG))
        self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_UNLISTED}", method="HEAD")[:2], (404, b""))

    def test_public_healthz_is_minimal(self):
        st, body, _ = req(self.pub, "/healthz")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    # ---------------------------------------------------------------- public: refused
    def test_public_404s_unlisted_and_bulk_sources(self):
        for path in (f"/img/ebay/{EBAY_UNLISTED}", f"/img/scp_catalog/{SCP_UNLISTED}", f"/img/fanatics/{FAN}",
                     f"/img/tcgplayer_catalog/{TCG}", "/card/baseball-cards-2020-topps/mike-trout-1",
                     "/r?u=https%3A//storage.googleapis.com/images.pricecharting.com/abc/240.jpg", "/"):
            self.assertEqual(req(self.pub, path)[0], 404, path)

    def test_public_404_is_identical_for_every_refusal(self):
        ref = req(self.pub, "/img/ebay/999999999999")              # listed nowhere, not on disk
        probes = [f"/img/ebay/{EBAY_UNLISTED}",                    # exists on disk, unlisted
                  f"/img/fanatics/{FAN}", f"/img/tcgplayer_catalog/{TCG}", "/img/nosuch/abc",
                  "/img/ebay/..", "/card/baseball-cards-2020-topps/mike-trout-1", "/r?u=http://x", "/",
                  "/.env", "/img/ebay", "/img/lotphoto_goldin/does-not-exist"]
        for p in probes:
            self.assertEqual(req(self.pub, p), ref, p)
        self.assertEqual(ref[0], 404)
        self.assertEqual(ref[1], b"not found\n")

    def test_public_denial_never_touches_disk_or_cache(self):
        calls = []
        orig = (cis.image_path, cis.cached_file)
        cis.image_path = lambda *a: calls.append(("image_path", a)) or None
        cis.cached_file = lambda *a: calls.append(("cached_file", a)) or None
        try:
            for p in (f"/img/ebay/{EBAY_UNLISTED}", f"/img/fanatics/{FAN}", "/img/ebay/123456789012"):
                self.assertEqual(req(self.pub, p)[0], 404)
        finally:
            cis.image_path, cis.cached_file = orig
        self.assertEqual(calls, [])

    def test_ssd_cache_never_bypasses_the_allowlist(self):
        # the internal door serves (and caches) an unlisted eBay photo ...
        self.assertEqual(req(self.internal, f"/img/ebay/{EBAY_UNLISTED}")[0], 200)
        self.assertIsNotNone(cis.cached_file("ebay", EBAY_UNLISTED))
        # ... and the public door still refuses it
        self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_UNLISTED}")[0], 404)

    def test_client_hangup_mid_image_is_clean(self):
        import socket as _s
        key = "big0000000000000001"
        put("scp_catalog", key, data=b"\xff\xd8\xff" + os.urandom(24 * 1024 * 1024))
        write_list(self.list_path, {"scp_catalog": {SCP_LISTED, key}, "ebay": {EBAY_LISTED},
                                    "lotphoto_goldin": {LOT_G}})
        self.allow.maybe_reload()
        self.access.lines.clear()
        c = _s.create_connection(("127.0.0.1", self.pub.server_address[1]), timeout=10)
        c.sendall(f"GET /img/scp_catalog/{key} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertIn(b"200", c.recv(64))
        c.setsockopt(_s.SOL_SOCKET, _s.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")   # RST on close
        c.close()
        self.assertIn(":aborted", self.access.wait_for(":aborted", timeout=10))
        self.assertIsNone(cis.cached_file("scp_catalog", key))       # a partial copy is never promoted
        leftovers = [n for _, _, ns in os.walk(cis.CACHE_DIR) for n in ns if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertEqual(req(self.pub, f"/img/scp_catalog/{SCP_LISTED}")[0], 200)   # server still fine

    def test_write_verbs_are_405_on_both_doors(self):
        for srv in (self.pub, self.internal):
            for verb in ("POST", "PUT", "DELETE", "PATCH"):
                self.assertEqual(req(srv, f"/img/scp_catalog/{SCP_LISTED}", method=verb)[0], 405)

    # ---------------------------------------------------------------- traversal
    def test_path_traversal_and_symlink_escape_blocked(self):
        probes = ["/img/ebay/..", "/img/ebay/.", "/img/ebay/%2e%2e", "/img/../ebay/" + EBAY_UNLISTED,
                  "/img/lotphoto_goldin/..%2Febay", "/img/lotphoto_goldin/evil",
                  "/img/scp_catalog/../../etc/passwd", "/img/lotphoto_goldin/" + ".." * 3]
        for srv in (self.pub, self.internal):
            for p in probes:
                self.assertEqual(req(srv, p)[0], 404, (srv.door, p))
        self.assertIsNone(cis.image_path("lotphoto_goldin", "evil"))
        self.assertIsNone(cis.image_path("ebay", ".."))
        self.assertIsNone(cis.image_path("ebay", "../ebay"))
        self.assertIsNone(cis.cached_file("ebay", ".."))

    # ---------------------------------------------------------------- spoofing
    def test_spoofed_headers_grant_nothing_on_public(self):
        spoof = {"Tailscale-User-Login": "andy@example.com", "Tailscale-User-Name": "Andy",
                 "Tailscale-User-Profile-Pic": "x", "Tailscale-Headers-Info": "x",
                 "X-Forwarded-For": "100.100.1.1", "X-Real-IP": "100.100.1.1", "Forwarded": "for=100.64.0.1",
                 "Host": "stavross-mac-mini.tail9fccf8.ts.net:8512"}
        for p in (f"/img/ebay/{EBAY_UNLISTED}", f"/img/fanatics/{FAN}", "/card/baseball-cards-2020-topps/mike-trout-1"):
            self.assertEqual(req(self.pub, p, headers=spoof)[0], 404, p)
        self.assertEqual(json.loads(req(self.pub, "/healthz", headers=spoof)[1]), {"ok": True})

    def test_internal_door_refuses_peers_outside_its_cidrs_even_with_headers(self):
        with self.assertRaises((http.client.HTTPException, ConnectionError, OSError)):
            req(self.internal_strict, f"/img/ebay/{EBAY_UNLISTED}",
                headers={"X-Forwarded-For": "100.101.1.1", "Tailscale-User-Login": "andy@example.com"})
        self.assertGreaterEqual(self.internal_strict.refused, 1)

    def test_peer_rules(self):
        nets = cis.parse_nets(cis.DEFAULT_INTERNAL_ALLOW)
        self.assertTrue(cis.peer_allowed("100.123.4.5", nets))
        self.assertTrue(cis.peer_allowed("fd7a:115c:a1e0::1234", nets))
        self.assertTrue(cis.peer_allowed("::ffff:100.64.0.9", nets))
        for bad in ("127.0.0.1", "::1", "192.168.1.20", "10.0.0.5", "208.111.34.5", "100.128.0.1", "", "junk"):
            self.assertFalse(cis.peer_allowed(bad, nets), bad)
        self.assertEqual(cis.peer_class("127.0.0.1"), "loopback")
        self.assertEqual(cis.peer_class("100.70.1.1"), "tailnet")
        self.assertEqual(cis.peer_class("192.168.1.9"), "lan")
        self.assertEqual(cis.peer_class("8.8.8.8"), "other")

    def test_bind_specs(self):
        self.assertEqual(cis.parse_bind("127.0.0.1:8510"), [("127.0.0.1", 8510)])
        self.assertEqual(cis.parse_bind("tailnet:8512,[fd7a::1]:8512"), [("tailnet", 8512), ("fd7a::1", 8512)])
        self.assertEqual(cis.parse_bind("off"), [])
        with self.assertRaises(ValueError):
            cis.parse_bind("8512")

    # ---------------------------------------------------------------- internal door
    def test_internal_door_keeps_full_behaviour(self):
        for p in (f"/img/ebay/{EBAY_UNLISTED}", f"/img/fanatics/{FAN}", f"/img/tcgplayer_catalog/{TCG}",
                  f"/img/scp_catalog/{SCP_UNLISTED}", "/card/baseball-cards-2020-topps/mike-trout-1"):
            self.assertEqual(req(self.internal, p)[0], 200, p)
        st, _, h = req(self.internal, "/r?u=https%3A//storage.googleapis.com/images.pricecharting.com/abc/240.jpg")
        self.assertEqual(st, 302)
        self.assertTrue(h["location"].startswith("/img/scp_catalog/"))
        st, body, _ = req(self.internal, "/")
        self.assertEqual(st, 200)
        self.assertNotIn(b"206162825097", body)          # no real eBay item id in the usage text any more
        st, body, _ = req(self.internal, "/healthz")
        d = json.loads(body)
        self.assertEqual(d["public"]["mode"], "enforce")
        self.assertTrue(d["public"]["allowlist"]["loaded"])
        self.assertIn("images", d)

    # ---------------------------------------------------------------- no list ever loaded
    def test_fail_closed_without_a_list_but_site_stays_up(self):
        self.assertFalse(self.nolist.loaded())
        self.assertEqual(req(self.pub_nolist, f"/img/scp_catalog/{SCP_UNLISTED}")[0], 200)
        self.assertEqual(req(self.pub_nolist, f"/img/lotphoto_goldin/{LOT_G}")[0], 200)
        for p in (f"/img/ebay/{EBAY_LISTED}", f"/img/ebay/{EBAY_UNLISTED}", f"/img/fanatics/{FAN}",
                  f"/img/tcgplayer_catalog/{TCG}"):
            self.assertEqual(req(self.pub_nolist, p)[0], 404, p)
        self.assertTrue(any("FAILING CLOSED" in m for m in self.nolist_logs), self.nolist_logs)

    # ---------------------------------------------------------------- observe mode
    def test_observe_mode_serves_but_logs_would_deny(self):
        self.access.lines.clear()
        self.assertEqual(req(self.pub_observe, f"/img/ebay/{EBAY_UNLISTED}")[0], 200)
        self.assertIn("would_deny:unlisted", self.access.wait_for("would_deny:unlisted"))

    # ---------------------------------------------------------------- logging
    def test_access_log_has_presence_flags_not_values_or_addresses(self):
        self.access.lines.clear()
        req(self.pub, f"/img/ebay/{EBAY_UNLISTED}",
            headers={"Tailscale-User-Login": "andy@example.com", "User-Agent": "Mozilla/5.0 Googlebot/2.1",
                     "Referer": "https://mazidex.com/card/x?secret=1"})
        req(self.pub, f"/img/scp_catalog/{SCP_LISTED}", headers={"Tailscale-Funnel-Request": "?1"})
        joined = self.access.wait_for("deny:unlisted", "allow:listed")
        self.assertIn("pub enforce deny:unlisted GET 404", joined)
        self.assertIn("pub enforce allow:listed GET 200", joined)
        self.assertIn("tsu=1 tsf=0 ua=googlebot ref=mazidex.com", joined)
        self.assertIn("tsu=0 tsf=1", joined)
        for leak in ("127.0.0.1", "andy@example.com", "secret=1"):
            self.assertNotIn(leak, joined)

    # ---------------------------------------------------------------- reload through the live server
    def test_reload_swaps_list_in_running_server(self):
        try:
            write_list(self.list_path, {"scp_catalog": {SCP_LISTED}, "ebay": {EBAY_UNLISTED}})
            self.allow.maybe_reload()
            self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_UNLISTED}")[0], 200)
            self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_LISTED}")[0], 404)
        finally:
            write_list(self.list_path, {"scp_catalog": {SCP_LISTED}, "ebay": {EBAY_LISTED},
                                        "lotphoto_goldin": {LOT_G}})
            self.allow.maybe_reload()
        self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_LISTED}")[0], 200)

    def test_staged_keys_are_public_until_unstaged(self):
        try:
            self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_STAGED}")[0], 404)
            with open(self.staged_path, "w") as f:
                f.write(f"# VeeFriends apply 2026-10-02\nhttps://{HOST}/img/ebay/{EBAY_STAGED}\n")
            bump(self.staged_path)
            self.allow.maybe_reload()
            self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_STAGED}")[0], 200)
            with open(self.staged_path, "w") as f:
                f.write("ebay/not a key\n")                 # bad edit: last good staged list stays
            bump(self.staged_path)
            self.allow.maybe_reload()
            self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_STAGED}")[0], 200)
            self.assertIsNotNone(self.allow.status["staged_error"])
        finally:
            os.unlink(self.staged_path)
            self.allow.maybe_reload()
        self.assertEqual(req(self.pub, f"/img/ebay/{EBAY_STAGED}")[0], 404)


class AllowListFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imgsrv_list_")
        self.path = os.path.join(self.dir, "l.txt")
        self.logs = []
        write_list(self.path, {"scp_catalog": {"k1", "k2"}, "ebay": {"111"}})
        self.al = cis.AllowList(self.path, None, log=self.logs.append)
        self.al.maybe_reload()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def assertGood(self):
        self.assertTrue(self.al.loaded())
        self.assertTrue(self.al.contains("scp_catalog", "k1"))
        self.assertTrue(self.al.contains("ebay", "111"))
        self.assertEqual(self.al.status["total"], 3)

    def overwrite(self, text):
        with open(self.path, "w") as f:
            f.write(text)
        bump(self.path)
        self.al.maybe_reload()

    def test_initial_load_and_counts(self):
        self.assertGood()
        self.assertEqual(self.al.status["counts"], {"scp_catalog": 2, "ebay": 1})
        self.assertTrue(any("public allow-list loaded: 3 keys" in m for m in self.logs))

    def test_reload_picks_up_changes(self):
        write_list(self.path, {"scp_catalog": {"k9"}})
        self.al.maybe_reload()
        self.assertTrue(self.al.contains("scp_catalog", "k9"))
        self.assertFalse(self.al.contains("scp_catalog", "k1"))
        self.assertFalse(self.al.contains("ebay", "111"))

    def test_truncated_file_keeps_last_good(self):
        good = cis.format_allowlist({"scp_catalog": {"k5", "k6"}})
        self.overwrite(good[: good.rindex("# end")])         # half-written: no trailer
        self.assertGood()
        self.assertIn("trailer", self.al.status["last_error"])

    def test_count_mismatch_keeps_last_good(self):
        self.overwrite(cis.ALLOWLIST_MAGIC + "\nscp_catalog/k5\n# end count=2\n")
        self.assertGood()

    def test_bad_entries_keep_last_good(self):
        for bad in ("nosuch/k5", "scp_catalog/..", "scp_catalog/a b", "ebay", "scp_catalog/" + "x" * 200):
            self.overwrite(f"{cis.ALLOWLIST_MAGIC}\n{bad}\n# end count=1\n")
            self.assertGood()

    def test_missing_header_or_content_after_trailer_keeps_last_good(self):
        self.overwrite("scp_catalog/k5\n# end count=1\n")
        self.assertGood()
        self.overwrite(f"{cis.ALLOWLIST_MAGIC}\nscp_catalog/k5\n# end count=1\nscp_catalog/k6\n")
        self.assertGood()
        self.overwrite("")
        self.assertGood()

    def test_deleted_file_keeps_last_good(self):
        os.unlink(self.path)
        self.al.maybe_reload()
        self.assertGood()
        self.assertTrue(any("disappeared" in m for m in self.logs))
        self.assertFalse(any("FAILING CLOSED" in m for m in self.logs))

    def test_never_loaded_decisions(self):
        logs = []
        al = cis.AllowList(os.path.join(self.dir, "nope.txt"), None, log=logs.append)
        al.maybe_reload()
        self.assertFalse(al.loaded())
        d = lambda s, k: cis.public_decision(s, k, al)[0]  # noqa: E731
        self.assertTrue(d("scp_catalog", "anykey"))
        self.assertTrue(d("lotphoto_goldin", "x1"))
        self.assertTrue(d("lotphoto_fanatics", "x1"))
        self.assertFalse(d("ebay", "111"))
        self.assertFalse(d("fanatics", "x1"))
        self.assertFalse(d("tcgplayer_catalog", "x1"))
        self.assertTrue(any("FAILING CLOSED" in m for m in logs))
        # a bad first file is the same as no file
        bad = os.path.join(self.dir, "bad.txt")
        with open(bad, "w") as f:
            f.write("garbage\n")
        al2 = cis.AllowList(bad, None, log=lambda m: None)
        al2.maybe_reload()
        self.assertFalse(al2.loaded())
        self.assertFalse(cis.public_decision("ebay", "111", al2)[0])

    def test_loaded_decisions(self):
        dec = lambda s, k: cis.public_decision(s, k, self.al)  # noqa: E731
        self.assertEqual(dec("scp_catalog", "k1"), (True, "listed"))
        self.assertEqual(dec("scp_catalog", "zz"), (False, "unlisted"))
        self.assertEqual(dec("ebay", "222"), (False, "unlisted"))
        self.assertEqual(dec("lotphoto_goldin", "zz"), (True, "curated_source"))
        self.assertEqual(dec("nosuch", "k1"), (False, "bad_request"))
        self.assertEqual(dec("scp_catalog", ".."), (False, "bad_request"))

    def test_staged_formats(self):
        got = cis.parse_staged(f"# c\n\nebay/123\nhttps://{HOST}/img/scp_catalog/abc\n/img/ebay/456/\n")
        self.assertEqual(got, {"ebay": frozenset({"123", "456"}), "scp_catalog": frozenset({"abc"})})
        for bad in ("ebay/12 3", "https://x/card/a/b", "nosuch/1", "justakey"):
            with self.assertRaises(cis.AllowListError):
                cis.parse_staged(bad)

    def test_format_roundtrip(self):
        entries = {"scp_catalog": {"b", "a"}, "ebay": {"1"}, "lotphoto_fanatics": {"x"}}
        sets, meta = cis.parse_allowlist(cis.format_allowlist(entries, {"built_at": "T"}))
        self.assertEqual({k: set(v) for k, v in sets.items() if v}, entries)
        self.assertEqual(meta["built_at"], "T")


class Builder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imgsrv_build_")
        self.out = __import__("pathlib").Path(self.dir) / "public_allowlist.txt"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, rows, **kw):
        args = dict(host=HOST, source_label="test", max_shrink=0.10, allow_shrink=False, min_keys=1,
                    dry_run=False, fail_on_other_routes=True)
        args.update(kw)
        return bia.build(rows, self.out, **args)

    def test_classify(self):
        self.assertEqual(bia.classify(f"https://{HOST}/img/scp_catalog/abc123"), ("ok", "scp_catalog", "abc123"))
        self.assertEqual(bia.classify(f"https://{HOST.upper()}/img/ebay/206162825097"), ("ok", "ebay", "206162825097"))
        self.assertEqual(bia.classify("https://pub-x.r2.dev/a.jpg"), ("other_host",))
        self.assertEqual(bia.classify(f"https://{HOST}/card/a/b"), ("our_host_other_route",))
        self.assertEqual(bia.classify(f"https://{HOST}/r?u=x"), ("our_host_other_route",))
        self.assertEqual(bia.classify(f"https://{HOST}/img/nosuch/abc"), ("unknown_source",))
        self.assertEqual(bia.classify(f"https://{HOST}/img/ebay/.."), ("bad_key",))
        self.assertEqual(bia.classify(None), ("empty",))

    def test_build_writes_a_file_the_server_loads(self):
        rows = [(f"https://{HOST}/img/scp_catalog/k1", f"https://{HOST}/img/scp_catalog/k1"),
                (f"https://{HOST}/img/ebay/111", None),
                (f"https://{HOST}/img/lotphoto_goldin/g1", f"https://{HOST}/img/lotphoto_goldin/g1"),
                ("https://elsewhere.example/img/ebay/999", None)]
        rc, s = self.build(rows)
        self.assertEqual(rc, 0, s)
        self.assertEqual(s["counts"], {"scp_catalog": 1, "ebay": 1, "lotphoto_goldin": 1})
        self.assertEqual(s["url_outcomes"]["other_host"], 1)
        al = cis.AllowList(str(self.out), None, log=lambda m: None)
        al.maybe_reload()
        self.assertTrue(al.contains("ebay", "111"))
        self.assertFalse(al.contains("ebay", "999"))
        self.assertEqual(bia.check(self.out), 0)
        self.assertEqual(bia.existing_count(self.out), 3)
        self.assertFalse([p for p in os.listdir(self.dir) if p.endswith(".tmp")])

    def test_dry_run_writes_nothing(self):
        rc, s = self.build([(f"https://{HOST}/img/ebay/1",)], dry_run=True)
        self.assertEqual(rc, 0)
        self.assertFalse(self.out.exists())

    def test_shrink_guard(self):
        many = [(f"https://{HOST}/img/scp_catalog/k{i}",) for i in range(100)]
        self.assertEqual(self.build(many)[0], 0)
        before = self.out.read_bytes()
        rc, s = self.build(many[:50])
        self.assertEqual(rc, 3)
        self.assertIn("shrink", s["refused"])
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(self.build(many[:95])[0], 0)                    # 5% is within the default 10%
        self.assertEqual(self.build(many[:10], allow_shrink=True)[0], 0)
        self.assertEqual(bia.existing_count(self.out), 10)

    def test_min_keys_and_other_route_guards(self):
        self.assertEqual(self.build([(f"https://{HOST}/img/ebay/1",)], min_keys=5)[0], 3)
        rows = [(f"https://{HOST}/img/ebay/1",), (f"https://{HOST}/card/a/b",)]
        self.assertEqual(self.build(rows)[0], 3)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.build(rows, fail_on_other_routes=False)[0], 0)

    def test_from_urls_files(self):
        src = os.path.join(self.dir, "urls.csv")
        with open(src, "w") as f:
            f.write(f'card_id,image_small\nmazi:vf:a,"https://{HOST}/img/ebay/206000000009"\n'
                    f"mazi:sc:b,https://{HOST}/img/scp_catalog/abcd\n")
        rows = list(bia.url_file_rows([src]))
        rc, s = self.build(rows)
        self.assertEqual((rc, s["counts"]), (0, {"scp_catalog": 1, "ebay": 1}))

    def test_keyset_chunks_visit_every_row_once(self):
        data = sorted((f"id{i:04d}", f"https://{HOST}/img/scp_catalog/k{i}" if i % 3 else None, None)
                      for i in range(1, 1001))

        class Cur:
            def __init__(self, rows):
                self.rows = rows

            def fetchone(self):
                return self.rows[0] if self.rows else None

            def fetchall(self):
                return self.rows

        class Conn:
            def __init__(self):
                self.statements = 0

            def transaction(self):
                import contextlib
                return contextlib.nullcontext()

            def execute(self, sql, params):
                self.statements += 1
                if sql == bia.BOUND_SQL:
                    after, off = params
                    ids = [r[0] for r in data if r[0] > after]
                    return Cur([(ids[off],)] if off < len(ids) else [])
                if sql == bia.RANGE_SQL:
                    lo, hi = params
                    return Cur([(r[1], r[2]) for r in data if lo < r[0] <= hi and (r[1] or r[2])])
                if sql == bia.TAIL_SQL:
                    (lo,) = params
                    return Cur([(r[1], r[2]) for r in data if lo < r[0] and (r[1] or r[2])])
                raise AssertionError(sql)

        got = list(bia.keyset_chunks(Conn(), chunk=70, pause=0))
        want = [(r[1], r[2]) for r in data if r[1] or r[2]]
        self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
