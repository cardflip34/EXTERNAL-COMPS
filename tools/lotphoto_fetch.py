#!/usr/bin/env python3
"""lotphoto_fetch.py -- download the lot photo of each published headline sale at Goldin / Fanatics Collect (2026-10-01).

Operator exception (CLAUDE.md Hard Gates, 2026-10-01; Andy: "yes, download the Goldin and Fanatics lot photos"): a
headline card may show the FRONT photo of its own Goldin or Fanatics lot as catalog art -- downloaded once, kept in our own
store under a separate removable prefix, never hot-linked, display only (never proof / evidence / binding / Trusted).
This tool does the download and storage side only; the two-reviewer exact-variant check and any beta apply are MAZIDEX's.

  store   /Volumes/MAZI_EVIDENCE_6TB/comp_images/lotphoto_<house>/<source id>/01.jpg  (one folder per house: removable)
  served  /img/lotphoto_<house>/<source id>   (comp_image_server: lotphoto_goldin / lotphoto_fanatics added to SOURCES)
  Goldin  https://d2tt46f3mh26nl.cloudfront.net/public/Lots/<lot_id>/<primary_image_name>@2x  (confirmed by a test fetch);
          older GD- rows: their stored raw.image_url, @1x -> @2x
  Fanatics the sales-history mediumImage1 path with /medium/ -> /large/ (the full scan, ~11 MB); the archived copy on disk
          (comp_images/fanatics/<id>/01.*) is the fallback.
  A photo over 600 KB is kept as orig.<ext> (never served) and 01.jpg is a DISPLAY copy: long edge 1200 px, JPEG q82, no
  crop, watermark untouched.

  python3 tools/lotphoto_fetch.py --dry-run     # list what it would fetch
  python3 tools/lotphoto_fetch.py               # fetch, ~1 s apart; writes manifest + failures JSON
"""
import argparse, glob, hashlib, json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

sys.path.insert(0, os.path.expanduser("~/whatnot-sniper"))
ROOT = "/Volumes/MAZI_EVIDENCE_6TB/comp_images"
EXPORT = os.path.expanduser("~/private/fixtures/headline_export")
OUT = os.path.expanduser("~/mazi_headline/lotphoto")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")          # the image server's key rule
DISPLAY_OVER = 600_000


def headline_rows():
    """Published headline sales at goldin / fanatics, from the loader manifests, minus anything since unpublished."""
    rows, unpub = {}, set()
    for f in glob.glob(os.path.join(EXPORT, "revert_*.json")):
        unpub |= set(json.load(open(f)).get("sale_ids") or [])
    for m in sorted(glob.glob(os.path.join(EXPORT, "manifest_*.json"))):
        mm = json.load(open(m))
        for r in json.load(open(mm["plan"]))["insert"]:
            if r["sale_id"] in mm["sale_ids"] and r["venue"] in ("goldin", "fanatics") and r["sale_id"] not in unpub:
                rows[r["sale_id"]] = {k: r[k] for k in ("sale_id", "card_id", "venue", "source_transaction_id", "source_url")}
    return list(rows.values())


def origins(rows):
    """sale_id -> candidate origin URLs, best first."""
    import psycopg
    from mazi_db.scripts.trusted_enrichment_spine import dsn_for_prod
    api = {}
    for f in glob.glob(os.path.expanduser("~/mazi_headline/fanatics_api_*.json")):
        for x in json.load(open(f)):
            api[str(x.get("source_id"))] = x
    raw = {}
    with psycopg.connect(dsn_for_prod(), connect_timeout=20) as c:
        c.execute("SET default_transaction_read_only = on")
        for code in ("goldin", "fanatics"):
            ids = [r["source_transaction_id"] for r in rows if r["venue"] == code]
            for sid, rw in c.execute("select source_item_id, raw from external_transactions where source_code = %s and source_item_id = any(%s)",
                                     (code, ids)):
                raw[(code, sid)] = rw or {}
        c.rollback()
    out = {}
    for r in rows:
        sid, rw = r["source_transaction_id"], raw.get((r["venue"], r["source_transaction_id"]), {})
        urls = []
        if r["venue"] == "goldin":
            if rw.get("lot_id") and rw.get("primary_image_name"):
                urls.append("https://d2tt46f3mh26nl.cloudfront.net/public/Lots/%s/%s@2x" % (rw["lot_id"], rw["primary_image_name"]))
            if rw.get("image_url"):
                urls += [re.sub(r"@1x$", "@2x", rw["image_url"]), rw["image_url"]]
        else:
            meds = [(api.get(sid) or {}).get("image"), rw.get("image_url")] + list(rw.get("image_urls") or [])[:1]
            for m in [x for x in meds if x]:
                urls += [m.replace("/medium/", "/large/"), m]
        out[r["sale_id"]] = list(dict.fromkeys(urls))
    return out


def dims(path):
    try:
        o = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path], capture_output=True, text=True, timeout=30).stdout
        w, h = re.search(r"pixelWidth: (\d+)", o), re.search(r"pixelHeight: (\d+)", o)
        return (int(w.group(1)) if w else None, int(h.group(1)) if h else None)
    except Exception:
        return (None, None)


def ext_of(body):
    return ".jpg" if body[:3] == b"\xff\xd8\xff" else ".png" if body[:8] == b"\x89PNG\r\n\x1a\n" else ".webp" if body[8:12] == b"WEBP" else None


def write_atomic(path, body):
    open(path + ".tmp", "wb").write(body)
    os.replace(path + ".tmp", path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.0)
    a = ap.parse_args()
    rows = headline_rows()
    urls = origins(rows)
    print(f"published headline sales: goldin {sum(r['venue'] == 'goldin' for r in rows)}, fanatics {sum(r['venue'] == 'fanatics' for r in rows)}; "
          f"with a candidate URL: {sum(1 for r in rows if urls.get(r['sale_id']))}", flush=True)
    if a.dry_run:
        for r in rows[:3] + [r for r in rows if r["venue"] == "fanatics"][:2]:
            print("  ", r["sale_id"][:44], "->", (urls.get(r["sale_id"]) or ["-"])[0][:120])
        return 0
    os.makedirs(OUT, exist_ok=True)
    manifest, failures = [], []
    for i, r in enumerate(rows, 1):
        house, key = r["venue"], r["source_transaction_id"]
        if not KEY_RE.match(key):
            failures.append({"sale_id": r["sale_id"], "card_id": r["card_id"], "house": house, "why": "source id is not a valid image key"})
            continue
        dest_dir = os.path.join(ROOT, "lotphoto_" + house, key)
        body, origin, why = None, None, "no candidate URL"
        for u in urls.get(r["sale_id"]) or []:
            try:
                resp = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=120)
                b = resp.read()
                time.sleep(a.sleep)
                if ext_of(b):
                    body, origin = b, u
                    break
                why = "not an image: %s" % resp.headers.get("Content-Type")
            except Exception as e:
                why = str(e)[:80]
                time.sleep(a.sleep)
        if body is None and house == "fanatics":                     # the archived copy, if any
            for base in (os.path.join(ROOT, "fanatics", key[:2], key[2:4], key), os.path.join(ROOT, "fanatics", key)):
                hit = sorted(f for f in (os.listdir(base) if os.path.isdir(base) else []) if re.fullmatch(r"\d{2}\.(jpg|jpeg|png|webp)", f))
                if hit:
                    body, origin = open(os.path.join(base, hit[0]), "rb").read(), "archive:comp_images/fanatics"
                    break
        if body is None:
            failures.append({"sale_id": r["sale_id"], "card_id": r["card_id"], "house": house, "why": why})
            continue
        os.makedirs(dest_dir, exist_ok=True)
        orig_bytes = None
        if len(body) > DISPLAY_OVER:          # a full scan: keep it as orig.*, serve a display copy as 01.jpg
            orig = os.path.join(dest_dir, "orig" + ext_of(body))
            write_atomic(orig, body)
            path = os.path.join(dest_dir, "01.jpg")
            subprocess.run(["sips", "-Z", "1200", "-s", "format", "jpeg", "-s", "formatOptions", "82", orig, "--out", path + ".tmp.jpg"],
                           capture_output=True, timeout=180, check=True)
            os.replace(path + ".tmp.jpg", path)
            orig_bytes, body = len(body), open(path, "rb").read()
        else:
            path = os.path.join(dest_dir, "01" + ext_of(body))
            write_atomic(path, body)
        w, h = dims(path)
        http = origin.startswith("http")
        manifest.append({"sale_id": r["sale_id"], "card_id": r["card_id"], "house": house, "source_id": key,
                         "served_path": f"/img/lotphoto_{house}/{key}", "local_path": path,
                         "sha256": hashlib.sha256(body).hexdigest(), "width": w, "height": h, "bytes": len(body),
                         "orig_bytes": orig_bytes, "origin_host": urlsplit(origin).netloc if http else origin,
                         "origin_path": urlsplit(origin).path if http else None, "lot_url": r.get("source_url")})
        if i % 20 == 0:
            print(f"  {i}/{len(rows)} done, {len(failures)} failed", flush=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mp, fp = os.path.join(OUT, f"lotphoto_manifest_{stamp}.json"), os.path.join(OUT, f"lotphoto_failures_{stamp}.json")
    json.dump(manifest, open(mp, "w"), indent=1)
    json.dump(failures, open(fp, "w"), indent=1)
    print(f"stored {len(manifest)} (goldin {sum(m['house'] == 'goldin' for m in manifest)}, fanatics "
          f"{sum(m['house'] == 'fanatics' for m in manifest)}), failed {len(failures)} -> {mp}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
