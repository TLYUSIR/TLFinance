#!/usr/bin/env python3
"""
Build the local search database for the school finance Q&A page.

1. Crawl the three EDB "財務管理" pages (and their sub-pages under the same
   section, up to MAX_DEPTH).
2. Download every PDF / DOC / DOCX they link to (conditional GET, cached).
3. Extract text, normalise CJK spacing, split into paragraph chunks.
4. Write site/data/index.js (window.EDB_INDEX = {...}) and index.json.

No AI / LLM involved. Run monthly (see scripts/update.sh).
Requires: Python 3.9+, pypdf. On macOS `textutil` handles .doc/.docx;
elsewhere .docx falls back to a built-in XML parser and .doc uses antiword
if installed.
"""
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(ROOT, "cache")
META_PATH = os.path.join(CACHE_DIR, "meta.json")
OUT_DIR = os.path.join(ROOT, "site", "data")

SEEDS = [
    "https://www.edb.gov.hk/tc/sch-admin/fin-management/about-fin-management/index.html",
    "https://www.edb.gov.hk/tc/sch-admin/fin-management/subsidy-info/index.html",
    "https://www.edb.gov.hk/tc/sch-admin/fin-management/notes-sch-fin/index.html",
]
CRAWL_PREFIX = "https://www.edb.gov.hk/tc/sch-admin/fin-management/"
MAX_DEPTH = 3
DOC_EXT = re.compile(r"\.(pdf|docx?)(?:[?#].*)?$", re.I)
UA = "Mozilla/5.0 (compatible; school-finance-qa-indexer/1.0)"

CHUNK_TARGET = 320   # characters
CHUNK_MAX = 520
CHUNK_MIN = 40


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def load_meta():
    if os.path.exists(META_PATH):
        with open(META_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_meta(meta):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)


def norm_url(u):
    u = html.unescape(u.strip())
    u = u.replace("http://www.edb.gov.hk", "https://www.edb.gov.hk")
    u = u.split("#")[0]
    # encode spaces / non-ascii so urllib accepts it, without double-encoding
    parts = urllib.parse.urlsplit(u)
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%()!$&'*+,;=:@")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def http_get(url, headers=None, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, dict(e.headers), b""
            last = e
            if e.code in (404, 403):
                break
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def fetch_page(url):
    status, _, body = http_get(url)
    return body.decode("utf-8", "ignore")


def cached_download(url, meta):
    """Download url into cache with If-None-Match / If-Modified-Since. Returns local path."""
    ext = DOC_EXT.search(url).group(1).lower()
    key = hashlib.sha1(url.encode()).hexdigest()
    path = os.path.join(CACHE_DIR, f"{key}.{ext}")
    entry = meta.get(url, {})
    headers = {}
    if os.path.exists(path):
        if entry.get("etag"):
            headers["If-None-Match"] = entry["etag"]
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = entry["last_modified"]
    status, resp, body = http_get(url, headers)
    if status == 304 and os.path.exists(path):
        return path, False
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)
    meta[url] = {
        "etag": resp.get("ETag"),
        "last_modified": resp.get("Last-Modified"),
        "size": len(body),
        "fetched": date.today().isoformat(),
    }
    return path, True


# --------------------------------------------------------------------------- #
# HTML parsing (EDB template)
# --------------------------------------------------------------------------- #
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(s):
    s = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</tr>|</div>", "\n", s, flags=re.I)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return s


GENERIC_ANCHOR = re.compile(r"^(?:\(?[ivx\d]{1,3}\)?|[一二三四五六七八九十]+|學校職員|學校校董|學校教職員|幼稚園校董及職員|資助中學|資助小學|資助特殊學校|綜合家具及設備津貼|擴大的營辦津貼|營辦津貼|附件\s?\S{1,3}|附錄\s?\S{1,3}|表格|指引|下載|按此|詳情|中文|English)$")
HEADING_RE = re.compile(r"<(strong|h[1-6]|em)\b[^>]*>(.*?)</\1>", re.S | re.I)
LABEL_RE = re.compile(r"^\s*[-–—]\s*(\S.{1,40}?)\s*$")


def _clean(s):
    return re.sub(r"\s+", " ", strip_tags(s)).replace("\xa0", " ").strip(" -–—/")


def parse_page(url, src):
    """Return (title, updated, [(href, descriptive title)], plain text) for an EDB page."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", src, re.S)
    title = strip_tags(m.group(1)).strip() if m else url
    i = src.find('<div class="generic_page_content">')
    j = src.find("<!-- Home Footer -->", i)
    body = src[i:j] if i >= 0 and j > i else ""
    m = re.search(r'id="sys_lastUpdateDate"\s+value="([^"]+)"', src)
    updated = m.group(1) if m else None

    # Walk the body in document order: remember the latest heading (<strong>/<hN>)
    # and "- label" lines so short anchors such as 學校職員 or (i) get their context.
    events = []
    for h in HEADING_RE.finditer(body):
        t = _clean(h.group(2))
        if t and not re.search(r"<a\s", h.group(2)) and not HEADING_RE.search(h.group(2)):
            events.append((h.start(), "sub" if h.group(1).lower() == "em" else "heading", t))
    for tbl in re.finditer(r"<table\b", body, re.I):
        events.append((tbl.start(), "table", ""))
    for cell in re.finditer(r"<(p|td|li)\b[^>]*>(.*?)</\1>", body, re.S | re.I):
        if "<a " in cell.group(2):
            continue
        lm = LABEL_RE.match(_clean(cell.group(2)))
        if lm:
            events.append((cell.start(), "label", lm.group(1)))
    for a in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        events.append((a.start(), "link", a))
    events.sort(key=lambda e: e[0])

    links, heading, sub, fresh_table = [], "", "", False
    for pos, kind, val in events:
        if kind == "table":
            fresh_table = True
        elif kind == "heading":
            heading, sub, fresh_table = val, "", False
        elif kind == "sub":
            if fresh_table or not heading:
                heading, sub, fresh_table = val, "", False
            else:
                sub = val
        elif kind == "label":
            sub = val
        else:
            fresh_table = False
            a = val
            href = norm_url(urllib.parse.urljoin(url, a.group(1)))
            anchor = _clean(a.group(2))
            tm = re.search(r'title="([^"]+)"', a.group(0))
            if not anchor and tm:
                anchor = _clean(tm.group(1))
            # text right after the link, e.g. 教育局通告第3/2022號 「學校及其教職員接受利益和捐贈事宜」
            tail = _clean(body[a.end():a.end() + 400].split("<a ")[0].split("</li>")[0].split("</td>")[0])
            qm = re.match(r"^[「《]([^」》]{2,60})[」》]", tail)
            name = anchor or os.path.basename(urllib.parse.unquote(href))
            if qm and qm.group(1) not in name:
                name = f"{name}「{qm.group(1)}」"
            elif GENERIC_ANCHOR.match(anchor or ""):
                ctx = " - ".join(x for x in (heading, sub) if x)
                if ctx:
                    name = f"{ctx}（{anchor}）"
            if name.count("《") > name.count("》"):
                name += "》"
            if name.count("「") > name.count("」"):
                name += "」"
            links.append((href, name))
    text = strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", "", body, flags=re.S))
    return title, updated, links, text


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
def extract_pdf(path):
    from pypdf import PdfReader  # noqa: WPS433
    pages = []
    reader = PdfReader(path)
    for n, page in enumerate(reader.pages, start=1):
        try:
            t = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            t = ""
        pages.append((n, t))
    return pages


def extract_docx_builtin(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"<w:tab/>", "\t", xml)
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    return html.unescape(TAG_RE.sub("", xml))


def extract_word(path):
    if shutil.which("textutil"):
        r = subprocess.run(["textutil", "-convert", "txt", "-stdout", path],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            return [(None, r.stdout.decode("utf-8", "ignore"))]
    if path.lower().endswith(".docx"):
        return [(None, extract_docx_builtin(path))]
    if shutil.which("antiword"):
        r = subprocess.run(["antiword", "-m", "UTF-8.txt", path], capture_output=True, timeout=120)
        if r.returncode == 0:
            return [(None, r.stdout.decode("utf-8", "ignore"))]
    raise RuntimeError("no .doc extractor available (install antiword or run on macOS)")


# --------------------------------------------------------------------------- #
# Normalisation & chunking
# --------------------------------------------------------------------------- #
CJK = "　-〿㐀-䶿一-鿿豈-﫿＀-￯‐-‧⺀-⻿"
CJK_CH = f"[{CJK}]"
SPACE_BETWEEN_CJK = re.compile(rf"(?<={CJK_CH})[ \t ]+(?={CJK_CH})")
SPACE_CJK_DIGIT = re.compile(rf"(?<={CJK_CH})[ \t]+(?=[0-9%])|(?<=[0-9%])[ \t]+(?={CJK_CH})")
SPACED_DIGITS = re.compile(r"(?<!\S)(\d(?: \d)+)(?!\S)")
SPACED_NUMBER_PUNCT = re.compile(r"(?<=\d) ?([,.]) ?(?=\d)")
PARA_START = re.compile(
    r"^\s*(?:"
    r"\d{1,2}(?:\.\d{1,2}){0,3}\.?\s"        # 1.  1.2  3.4.5
    r"|\(?[a-zA-Z0-9]{1,3}\)\s?"             # (a) (12) a)
    r"|\(?[ivx]{1,4}\)\s?"                   # (iv)
    r"|[•⚫●■◆▪◦※＊*‧·\-–]\s?"                # bullets
    r"|[甲乙丙丁戊己庚辛壬癸][部、\.．]"
    r"|[一二三四五六七八九十]{1,3}[、\.．]"
    r"|\([一二三四五六七八九十]{1,3}\)"
    r"|附[件錄]\s?[一二三四五六七八九十IVX\d]+"
    r"|第[一二三四五六七八九十\d]+[章節條部]"
    r")",
    re.U,
)
PAGE_NO_LINE = re.compile(r"^\s*[-–—]?\s*\d{1,3}\s*[-–—]?\s*$")
SENT_END = re.compile(r"(?<=[。；！？!?])")


def normalise(text):
    text = text.replace("\r", "")
    text = text.replace("　", " ")
    text = SPACE_BETWEEN_CJK.sub("", text)
    text = SPACE_BETWEEN_CJK.sub("", text)
    text = SPACED_DIGITS.sub(lambda m: m.group(1).replace(" ", ""), text)
    text = SPACED_NUMBER_PUNCT.sub(r"\1", text)
    text = SPACE_CJK_DIGIT.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def lines_to_paragraphs(text):
    """Join wrapped lines into paragraphs; start a new one on blank line or list marker."""
    paras, cur = [], ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or PAGE_NO_LINE.match(line):
            if cur:
                paras.append(cur)
                cur = ""
            continue
        if cur and PARA_START.match(line):
            paras.append(cur)
            cur = line
            continue
        if not cur:
            cur = line
        elif re.search(rf"{CJK_CH}$", cur) and re.match(rf"^{CJK_CH}", line):
            cur += line
        elif re.search(r"[A-Za-z0-9,]$", cur) and re.match(r"^[A-Za-z0-9]", line):
            cur += " " + line
        else:
            cur += ("" if re.search(rf"{CJK_CH}$", cur) or re.match(rf"^{CJK_CH}", line) else " ") + line
    if cur:
        paras.append(cur)
    return [normalise(p).strip() for p in paras if p.strip()]


def split_long(p):
    if len(p) <= CHUNK_MAX:
        return [p]
    out, cur = [], ""
    for s in SENT_END.split(p):
        if cur and len(cur) + len(s) > CHUNK_TARGET:
            out.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        out.append(cur)
    # hard split anything still too long
    final = []
    for c in out:
        while len(c) > CHUNK_MAX:
            final.append(c[:CHUNK_MAX])
            c = c[CHUNK_MAX:]
        final.append(c)
    return final


def make_chunks(paras):
    """Greedy-merge short paragraphs up to CHUNK_TARGET; split very long ones."""
    chunks, cur = [], ""
    for p in paras:
        for piece in split_long(p):
            if cur and len(cur) + len(piece) + 1 > CHUNK_TARGET:
                chunks.append(cur)
                cur = piece
            else:
                cur = (cur + "\n" + piece) if cur else piece
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(re.sub(r"\s", "", c)) >= CHUNK_MIN]


def chunks_from_pages(pages):
    """pages: list of (page_no|None, text). Returns list of (page_no, chunk)."""
    out = []
    for page_no, text in pages:
        for c in make_chunks(lines_to_paragraphs(text)):
            out.append((page_no, c))
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    started = time.time()
    meta = load_meta()
    sources, chunks = [], []
    seen_pages, seen_docs = set(), {}
    queue = [(u, 0) for u in SEEDS]
    page_count = 0

    while queue:
        url, depth = queue.pop(0)
        url = norm_url(url)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        try:
            src = fetch_page(url)
        except Exception as e:  # noqa: BLE001
            print(f"[page] FAIL {url}: {e}", file=sys.stderr)
            continue
        title, updated, links, text = parse_page(url, src)
        page_count += 1
        print(f"[page] {title} ({url})")
        sid = len(sources)
        sources.append({"id": sid, "type": "web", "title": title, "url": url,
                        "page_title": title, "page_url": url, "updated": updated})
        # index pages that are only a list of links carry no answers; keep pages with real prose
        if len(re.sub(r"\s", "", text)) >= 200:
            for _, c in chunks_from_pages([(None, text)]):
                chunks.append({"s": sid, "p": None, "t": c})
        for href, anchor in links:
            if DOC_EXT.search(href) and "edb.gov.hk" in href:
                if href not in seen_docs:
                    seen_docs[href] = {"anchor": anchor or os.path.basename(urllib.parse.unquote(href)),
                                       "page_title": title, "page_url": url}
            elif href.startswith(CRAWL_PREFIX) and depth < MAX_DEPTH and href not in seen_pages:
                queue.append((href, depth + 1))

    ok = fail = 0
    for url, info in seen_docs.items():
        try:
            path, fresh = cached_download(url, meta)
            if path.lower().endswith(".pdf"):
                pages = extract_pdf(path)
            else:
                pages = extract_word(path)
            doc_chunks = chunks_from_pages(pages)
            if not doc_chunks:
                print(f"[doc ] EMPTY {info['anchor']} ({url})", file=sys.stderr)
                continue
            sid = len(sources)
            sources.append({"id": sid, "type": DOC_EXT.search(url).group(1).lower(),
                            "title": info["anchor"], "url": url,
                            "page_title": info["page_title"], "page_url": info["page_url"],
                            "pages": len(pages) if pages and pages[0][0] else None})
            for p, c in doc_chunks:
                chunks.append({"s": sid, "p": p, "t": c})
            ok += 1
            print(f"[doc ] {'new ' if fresh else 'same'} {len(doc_chunks):4d} chunks  {info['anchor']}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"[doc ] FAIL {info['anchor']} ({url}): {e}", file=sys.stderr)

    save_meta(meta)
    index = {
        "built": date.today().isoformat(),
        "seeds": SEEDS,
        "counts": {"pages": page_count, "documents": ok, "chunks": len(chunks)},
        "sources": sources,
        "chunks": chunks,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    payload = json.dumps(index, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(OUT_DIR, "index.json"), "w", encoding="utf-8") as f:
        f.write(payload)
    with open(os.path.join(OUT_DIR, "index.js"), "w", encoding="utf-8") as f:
        f.write("window.EDB_INDEX=" + payload + ";\n")
    print(f"\nDone in {time.time() - started:.0f}s: {page_count} pages, {ok} documents "
          f"({fail} failed), {len(chunks)} chunks, {len(payload) / 1e6:.1f} MB -> {OUT_DIR}")
    return 1 if (ok == 0) else 0


if __name__ == "__main__":
    sys.exit(main())
