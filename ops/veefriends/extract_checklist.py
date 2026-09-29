"""Generic checklist extractor: HTML -> [(section, code, name)] using the last heading seen."""
import html as H, re, sys, collections

CARD = re.compile(r"^(?P<code>(?:[A-Z]{1,6}-)?\d{1,3}[a-z]?)\s+(?P<name>[A-Z0-9][^\n]{1,70})$")
NOISE = re.compile(r"(Total Cards|Refractor\s*/|\bodds\b|\d:\d|Hobby|Blaster|Mega|Box|Pack)", re.I)

SKIP_HEAD = re.compile(r"^(Buy on eBay|Buy on|Shop|Parallels?:?|Pack odds|Autograph Parallels?:?)", re.I)

def lines_with_heads(h):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h)
    # table layouts (cardsmithsbreaks): <td class="n">37</td><td class="pcell"><span class="pl">Generous Gerbil</span>
    h = re.sub(r'(?is)<td class="n">\s*([^<]+?)\s*</td>\s*<td class="pcell">\s*<span class="pl">\s*([^<]+?)\s*</span>', r"<br>\1 \2<br>", h)
    h = re.sub(r"(?i)<(h[1-4]|strong|b)(\s[^>]*)?>", "\n@@HEAD@@", h)
    h = re.sub(r"(?i)</(h[1-4]|strong|b)>", "\n", h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|td|h[1-6])>", "\n", h)
    t = H.unescape(re.sub(r"<[^>]+>", " ", h))
    for l in t.splitlines():
        l = re.sub(r"\s+", " ", l).strip()
        if l: yield l

def extract(path):
    sec, out = "?", []
    for l in lines_with_heads(open(path, errors="ignore").read()):
        if l.startswith("@@HEAD@@"):
            s = l[8:].strip()
            if s and len(s) < 90 and not SKIP_HEAD.match(s): sec = s
            continue
        m = CARD.match(l)
        if m and not NOISE.search(l):
            out.append((sec, m.group("code"), m.group("name").strip()))
    return out

if __name__ == "__main__":
    rows = extract(sys.argv[1])
    c = collections.OrderedDict()
    for s, code, name in rows: c.setdefault(s, []).append(f"{code} {name}")
    print(len(rows), "rows in", len(c), "sections")
    for s, v in c.items(): print(f"  [{len(v):3d}] {s[:70]}  e.g. {v[0][:40]} ... {v[-1][:40]}")
