#!/usr/bin/env python3
"""ebay_signed_in_sweep.py -- signed-in eBay SOLD sweep for Pokemon + sports (Andy 2026-09-29: "maximize scraping ...
mitigate the risk of detection but don't let it be a reason not to scrub").

eBay shows sold listings only to signed-in accounts (since ~2026-07-01) and only for 90 days, so every day of delay loses
a day of sales for good. This walks eBay's own sold search in the dedicated account's REAL Chrome (the window
~/mazi_ebay_session/sign_in.sh opened; we attach over its 127.0.0.1 debug port and open ONE tab of our own).

  plan    split a segment into price-band SHARDS small enough to walk to the 90-day floor (bisects on eBay's own
          result count; a band still too big at $0.25 wide is kept as 'capped' and walked newest-first only)
  run     walk shards newest-first, highest price band first, then keep them fresh (re-walk until a page adds nothing)
  status  progress per segment

Detection hygiene (pacing only -- no fingerprint tricks, no CAPTCHA solving): one tab, randomized 10-22 s between
pages, a 3-9 min break every 50-90 pages, a daily page cap, pause while the VeeFriends daily job uses the browser or
the Mini is RED, and a HALT on a login wall (needs a human re-sign-in) or a CAPTCHA/block page (cool down 2 h, then 4 h,
then stop for the day).

Output: /Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_signed_in/<segment>/sold_<UTC date>.jsonl (one row per sold
listing, first sighting only). State (small): ~/mazi_ebay_sweep/state.sqlite. Neon import is a separate step
(tools/import_ebay_sweep.py). Launch from ssh (sshd has disk access to the 6 TB volume):
  nohup /usr/bin/python3 -u tools/ebay_signed_in_sweep.py run --segments pokemon >> ~/mazi_ebay_sweep/sweep.log 2>&1 &
"""
from __future__ import annotations

import argparse, json, os, random, re, sqlite3, subprocess, sys, time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "external_engine"), os.path.join(ROOT, "tools")):
    sys.path.insert(0, p)

CDP = "http://127.0.0.1:9333"
HOME = os.path.expanduser("~/mazi_ebay_sweep")
DB = os.path.join(HOME, "state.sqlite")
OUT = "/Volumes/MAZI_EVIDENCE_6TB/whatnot-sniper/ebay_signed_in"
SEGMENTS = {  # segment -> (eBay category, keyword). Categories keep boxes/packs/lots of other kinds out.
    "pokemon": ("183454", "pokemon"),                       # CCG Individual Cards
    "sports": ("261328", "(topps,panini,bowman,donruss,upper deck,fleer,leaf,score,prizm,select,optic,chrome)"),
}
CAP = 9_500              # walk a shard fully only when eBay counts at most this many results (~40 pages of 240)
MIN_BAND = 0.25          # narrowest price band worth splitting to
PRICE_TOP = 100_000.0
LADDER = [0, 1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 75, 100, 150, 200, 300, 500, 1000, 2500, PRICE_TOP]   # split further only if too big
ITEM_RE = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,14})")
COUNT_RE = re.compile(r"([\d,]+)\+?\s+results?", re.I)


class Halt(RuntimeError):
    pass


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
      CREATE TABLE IF NOT EXISTS shards (id INTEGER PRIMARY KEY, segment TEXT, lo REAL, hi REAL, est INTEGER,
        status TEXT DEFAULT 'planned', pages INTEGER DEFAULT 0, rows INTEGER DEFAULT 0, next_page INTEGER DEFAULT 1,
        last_walk TEXT, UNIQUE(segment, lo, hi));
      CREATE TABLE IF NOT EXISTS seen (item_id TEXT PRIMARY KEY, segment TEXT, sold_date TEXT);
      CREATE TABLE IF NOT EXISTS events (at TEXT, kind TEXT, detail TEXT);
      CREATE TABLE IF NOT EXISTS daily (day TEXT PRIMARY KEY, pages INTEGER DEFAULT 0, rows INTEGER DEFAULT 0, halts INTEGER DEFAULT 0);
    """)
    return c


def event(c, kind, detail):
    c.execute("INSERT INTO events VALUES (?,?,?)", (now_iso(), kind, detail[:500])); c.commit()
    print(f"[{now_iso()}] {kind}: {detail}", flush=True)


def url(segment, lo, hi, page, ipg=240):
    cat, kw = SEGMENTS[segment]
    q = {"_nkw": kw, "LH_Sold": 1, "LH_Complete": 1, "_sop": 13, "_ipg": ipg, "_pgn": page}
    if lo is not None:
        q["_udlo"] = f"{lo:.2f}"
    if hi is not None:
        q["_udhi"] = f"{hi:.2f}"
    return f"https://www.ebay.com/sch/{cat}/i.html?" + urlencode(q)


class Browser:
    """Our one tab in the signed-in Chrome, with the pacing and halt rules."""
    def __init__(self, c, args):
        from playwright.sync_api import sync_playwright
        from ebay_bulk_scraper import extract_listings, block_signature
        from ebay_player_scraper import parse_listing_sold_date
        from ebay_polite_adapter import _price
        self.extract, self.block, self.parse_date, self.price = extract_listings, block_signature, parse_listing_sold_date, _price
        self.c, self.args = c, args
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.connect_over_cdp(args.cdp)
            self.page = self.browser.contexts[0].new_page()
        except BaseException:
            # A failed connect left Playwright started. cmd_run's `finally` only closes a Browser it received, so
            # without this stop every later retry in the process failed with "Playwright Sync API inside the asyncio
            # loop": the sports sweep sat dead that way from 2026-09-29, Pokemon from 10-02.
            self.pw.stop()
            raise
        self.loads, self.next_break = 0, random.randint(args.break_every_min, args.break_every_max)

    def close(self):
        try:
            self.page.close()
        finally:
            self.pw.stop()

    def pace(self):
        if self.loads == 0:
            return
        if self.loads >= self.next_break:
            nap = random.uniform(self.args.break_min_s, self.args.break_max_s)
            print(f"[{now_iso()}] break {nap/60:.1f} min after {self.loads} pages", flush=True)
            time.sleep(nap); self.next_break = self.loads + random.randint(self.args.break_every_min, self.args.break_every_max)
        else:
            time.sleep(random.uniform(self.args.min_delay, self.args.max_delay))
        wait_for_turn(self.c, self.args.account)

    def load(self, link):
        self.pace()
        self.page.goto(link, timeout=60000, wait_until="domcontentloaded")
        self.page.wait_for_timeout(random.randint(1200, 2600))
        self.loads += 1
        self.c.execute("INSERT INTO daily(day, pages) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET pages = pages + 1", (day_key(self.args),)); self.c.commit()
        if "signin.ebay.com" in self.page.url:
            self.snapshot("login"); raise Halt("LOGIN_REQUIRED")
        sig = self.block(self.page.evaluate("() => document.body.innerText.substring(0, 800)") or "")
        if sig in ("blocked", "captcha"):
            self.snapshot(sig); raise Halt(sig.upper())

    def snapshot(self, why):
        """What eBay actually showed when we halted (text only, no cookies) -- to tell a real block from a misread."""
        try:
            txt = self.page.evaluate("() => document.body.innerText.substring(0, 3000)") or ""
            with open(os.path.join(HOME, f"halt_{self.args.account}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{why}.txt"), "w") as f:
                f.write(f"url: {self.page.url}\n\n{txt}")
        except Exception:
            pass

    def count(self, link):
        self.load(link)
        for sel in (".srp-controls__count-heading", ".result-count__count-heading"):
            el = self.page.query_selector(sel)
            if el:
                m = COUNT_RE.search(el.inner_text())
                if m:
                    return int(m.group(1).replace(",", ""))
        return 0 if "No exact matches" in (self.page.inner_text("body")[:3000] or "") else None

    def rows(self, segment, shard_id, pno):
        out, dates = [], []
        for it in self.extract(self.page) or []:
            m = ITEM_RE.search(it.get("link") or "")
            sd = self.parse_date(it.get("soldDate") or "")
            if sd:
                dates.append(sd)
            if not m or not sd:
                continue
            out.append({"item_id": m.group(1), "title": it.get("title"), "sold_price": self.price(it.get("priceText")),
                        "sold_date": sd, "best_offer": bool(it.get("bestOffer")), "shipping": it.get("shipping") or "",
                        "condition": it.get("condition") or "", "bids": it.get("bids") or "", "url": it.get("link"),
                        "image_url": it.get("imgUrl") or "", "segment": segment, "ebay_category": SEGMENTS[segment][0],
                        "shard": shard_id, "page": pno, "scraped_at": now_iso(), "source": "ebay_signed_in_sweep_v1"})
        return out, dates


def day_key(args):
    """Daily page caps are per eBay account ('mini' = the Mini's window, 'm4' = the M4's)."""
    return f"{date.today().isoformat()}:{getattr(args, 'account', 'mini')}"


def wait_for_turn(c, account="mini"):
    """Mini account only: wait while the VeeFriends daily job uses that browser; pause while the Mini is RED.
    The M4 account's browser runs on the M4 (this script only drives it), so neither applies there."""
    if account != "mini":
        return
    import resource_governor as rg
    while True:
        busy = subprocess.run(["/usr/bin/pgrep", "-f", "ebay_veefriends_pilot.py"], capture_output=True).returncode == 0
        level = rg.classify(rg.sample())[0]
        if not busy and level != "RED":
            return
        time.sleep(120)


def plan(c, br, segment):
    """Bisect [0, PRICE_TOP) by eBay's own count until each band <= CAP (or is MIN_BAND wide -> 'capped')."""
    stack, made = [(float(a), float(b)) for a, b in zip(LADDER, LADDER[1:])], 0      # popped high band first
    while stack:
        lo, hi = stack.pop()
        if c.execute("SELECT 1 FROM shards WHERE segment=? AND lo=? AND hi=?", (segment, lo, hi)).fetchone():
            continue
        n = br.count(url(segment, lo, hi, 1, ipg=60))
        if n is None:
            event(c, "plan_warn", f"{segment} {lo}-{hi}: no count header"); n = CAP + 1
        if n > CAP and hi - lo > MIN_BAND:
            mid = round((lo + hi) / 2, 2)
            stack += [(lo, mid), (mid, hi)]
            continue
        c.execute("INSERT OR IGNORE INTO shards(segment, lo, hi, est, status) VALUES (?,?,?,?,?)",
                  (segment, lo, hi, n, "planned" if n <= CAP else "capped")); c.commit()
        made += 1
        print(f"[{now_iso()}] shard {segment} ${lo:.2f}-${hi:.2f}: {n:,} results", flush=True)
    return made


def walk(c, br, shard, out_dir, floor, max_pages):
    sid, segment, lo, hi, status, next_page, est = shard
    last_useful = (est // 240 + 2) if est else None          # eBay pads past the real results with repeats/fillers
    dry = 0
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    os.makedirs(os.path.join(out_dir, segment), exist_ok=True)
    fresh = status == "done"                     # re-walk for new sales: stop at the first page that adds nothing
    pno = 1 if fresh else next_page
    added_total, pages = 0, 0
    with open(os.path.join(out_dir, segment, f"sold_{day}.jsonl"), "a") as f:
        while pages < max_pages:
            br.load(url(segment, lo, hi, pno)); pages += 1
            rows, dates = br.rows(segment, sid, pno)
            # keep EVERY sale eBay shows (it serves history older than 90 days to a signed-in account); the floor only
            # decides when a walk stops
            new = [r for r in rows if not c.execute("SELECT 1 FROM seen WHERE item_id=?", (r["item_id"],)).fetchone()]
            for r in new:
                c.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?)", (r["item_id"], segment, r["sold_date"]))
                f.write(json.dumps(r) + "\n")
            f.flush(); c.commit()
            added_total += len(new)
            c.execute("UPDATE daily SET rows = rows + ? WHERE day = ?", (len(new), day_key(br.args)))
            old_share = sum(1 for d in dates if d < floor) / max(len(dates), 1)
            dry = 0 if new else dry + 1
            end = (not rows or old_share >= 0.9 or (fresh and not new) or len(rows) < 120 or dry >= 2
                   or (last_useful is not None and status != "capped" and pno >= last_useful))
            c.execute("UPDATE shards SET pages = pages + 1, rows = rows + ?, next_page = ?, last_walk = ?, status = ? WHERE id = ?",
                      (len(new), 1 if end else pno + 1, now_iso(), "done" if end else ("walking" if status != "capped" else "capped"), sid))
            c.commit()
            print(f"[{now_iso()}] {segment} ${lo:.2f}-${hi:.2f} p{pno}: {len(rows)} rows, {len(new)} new, oldest {min(dates) if dates else '-'}", flush=True)
            if end:
                break
            pno += 1
    return added_total


def cmd_run(args):
    c = db()
    halts, cool = 0, [7200, 14400]
    while True:
        used = (c.execute("SELECT pages FROM daily WHERE day=?", (day_key(args),)).fetchone() or [0])[0]
        if used >= args.daily_pages:
            event(c, "daily_cap", f"{used} pages today; sleeping to tomorrow"); time.sleep(1800); continue
        br = None
        try:
            br = Browser(c, args)
            for seg in args.segments:
                if not c.execute("SELECT 1 FROM shards WHERE segment=?", (seg,)).fetchone():
                    event(c, "plan_start", seg); event(c, "plan_done", f"{seg}: {plan(c, br, seg)} shards")
            floor = (date.today() - timedelta(days=args.floor_days)).isoformat()
            # first sweep: never-walked or unfinished shards, most valuable (highest price) first; then freshness passes
            q = """SELECT id, segment, lo, hi, status, next_page, est FROM shards WHERE segment IN (%s)
                   ORDER BY CASE WHEN status IN ('planned','walking') THEN 0 WHEN status='capped' AND last_walk IS NULL THEN 1 ELSE 2 END,
                            CASE WHEN status IN ('done','capped') THEN COALESCE(last_walk, '') END, hi DESC""" % ",".join("?" * len(args.segments))
            shards = c.execute(q, args.segments).fetchall()
            for sh in shards:
                if (c.execute("SELECT pages FROM daily WHERE day=?", (day_key(args),)).fetchone() or [0])[0] >= args.daily_pages:
                    break
                walk(c, br, sh, OUT, floor, args.shard_pages)
            halts = 0
        except Halt as h:
            c.execute("INSERT INTO daily(day, halts) VALUES (?,1) ON CONFLICT(day) DO UPDATE SET halts = halts + 1", (day_key(args),)); c.commit()
            if str(h) == "LOGIN_REQUIRED":
                event(c, "HALT", f"[{args.account}] eBay signed us out -- re-run that machine's sign_in.sh and sign in; the sweep stops until restarted")
                return 2
            wait = cool[halts] if halts < len(cool) else None
            halts += 1
            if wait is None:
                event(c, "HALT", f"{h} three times -- stopping for today"); time.sleep(6 * 3600); halts = 0; continue
            event(c, "cooldown", f"{h}: pausing {wait // 3600} h (halt {halts})"); time.sleep(wait)
        except Exception as e:
            event(c, "error", f"{type(e).__name__}: {e}"); time.sleep(300)
        finally:
            if br:
                try:
                    br.close()
                except Exception:
                    pass
        if args.once:
            return 0


def cmd_test(args):
    """Count one band and parse two pages into a TEST file (not recorded as seen, not imported)."""
    c = db()
    br = Browser(c, args)
    try:
        n = br.count(url(args.segment, args.lo, args.hi, 1, ipg=60))
        print(f"{args.segment} ${args.lo}-{args.hi}: eBay count {n}")
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, "test_rows.jsonl"), "w") as f:
            for pno in (1, 2):
                br.load(url(args.segment, args.lo, args.hi, pno))
                rows, dates = br.rows(args.segment, 0, pno)
                for r in rows:
                    f.write(json.dumps(r) + "\n")
                print(f"  page {pno}: {len(rows)} rows, dates {min(dates) if dates else '-'} .. {max(dates) if dates else '-'}, "
                      f"best offer {sum(r['best_offer'] for r in rows)}, e.g. {rows[0]['title'][:60] if rows else ''} ${rows[0]['sold_price'] if rows else ''}")
    except Halt as h:
        print("HALT:", h); return 2
    finally:
        br.close()
    return 0


def cmd_status(args):
    c = db()
    for seg, n, done, capped, rows, pages in c.execute("""SELECT segment, count(*), sum(status='done'), sum(status='capped'), sum(rows), sum(pages)
                                                          FROM shards GROUP BY segment"""):
        print(f"{seg}: {n} shards ({done} done, {capped} capped), {rows or 0:,} rows from {pages or 0:,} pages")
    for d in c.execute("SELECT day, pages, rows, halts FROM daily ORDER BY day DESC LIMIT 5"):
        print("  day", d)
    for e in c.execute("SELECT at, kind, detail FROM events ORDER BY rowid DESC LIMIT 6"):
        print("  event", e)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--segments", type=lambda s: s.split(","), default=["pokemon"])
    r.add_argument("--daily-pages", type=int, default=2500)
    r.add_argument("--shard-pages", type=int, default=45)
    r.add_argument("--floor-days", type=int, default=90)
    r.add_argument("--min-delay", type=float, default=18.0)
    r.add_argument("--max-delay", type=float, default=40.0)
    r.add_argument("--once", action="store_true", help="one pass over the shards, then exit")
    r.add_argument("--account", default="mini", help="which eBay account/window: mini (127.0.0.1:9333) or m4 (tunnelled to 9334)")
    r.add_argument("--break-every-min", type=int, default=40); r.add_argument("--break-every-max", type=int, default=70)
    r.add_argument("--break-min-s", type=float, default=240.0); r.add_argument("--break-max-s", type=float, default=720.0)
    r.add_argument("--cdp", default=CDP)
    sub.add_parser("status")
    t = sub.add_parser("test")
    t.add_argument("--segment", default="pokemon"); t.add_argument("--lo", type=float, default=500.0); t.add_argument("--hi", type=float, default=1000.0)
    t.add_argument("--min-delay", type=float, default=10.0); t.add_argument("--max-delay", type=float, default=22.0)
    t.add_argument("--account", default="mini"); t.add_argument("--cdp", default=CDP)
    t.add_argument("--break-every-min", type=int, default=40); t.add_argument("--break-every-max", type=int, default=70)
    t.add_argument("--break-min-s", type=float, default=240.0); t.add_argument("--break-max-s", type=float, default=720.0)
    a = ap.parse_args()
    return {"run": cmd_run, "status": cmd_status, "test": cmd_test}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
