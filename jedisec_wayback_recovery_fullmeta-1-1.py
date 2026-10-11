#!/usr/bin/env python3
"""Resumable multi-source live/Wayback inventory and recovery worker.

Wayback mode uses the Wayback URL index (CDX API), paginates through every result
page for EVERY configured root, and stores every URL/capture row before classifying
HTML pages and downloadable files. Progress is checkpointed after each page.
Live mode recursively follows same-host subfolders under the configured root.
No recovered file is executed, extracted, or imported into a database.

Examples:
  python3 jedisec_wayback_recovery.py --source wayback --out ./jedi-recovery
  python3 jedisec_wayback_recovery.py --source wayback --download --out ./jedi-recovery
  python3 jedisec_wayback_recovery.py --source wayback --root https://example.org/archive/ --out ./recovery
  python3 jedisec_wayback_recovery.py --source live --root https://jedi-sec.us/downloads/ --out ./jedi-recovery
"""
import argparse, csv, hashlib, html.parser, json, mimetypes, os, random, sqlite3, sys, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

LIVE_ROOT = "https://jedi-sec.us/downloads/"
DEFAULT_WAYBACK_ROOTS = [
    "https://jedi-sec.us/",
    "https://jedi-sec.us/downloads/",
    "http://oss.navy/",
    "http://clintonsprivateserver.com/",
    "http://jedi-sec.space/",
    "http://jedisecx.com/",
]
CDX_URL = "https://web.archive.org/cdx/search/cdx"
EXTENSIONS = {
    ".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
    ".sql", ".sqlite", ".sqlite3", ".db", ".dump", ".bak"
}
UA = "JediSec-ArchiveResearch/1.2 (polite resumable inventory)"

class LinkParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href: self.links.append(href)

class MetaParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.in_title = False
        self.meta = {}
        self.canonical = ""
        self.html_lang = ""
    def handle_starttag(self, tag, attrs):
        attrs = {str(k).lower(): (v or "") for k, v in attrs}
        if tag.lower() == "html":
            self.html_lang = attrs.get("lang", "")
        elif tag.lower() == "title":
            self.in_title = True
        elif tag.lower() == "meta":
            key = (attrs.get("name") or attrs.get("property") or attrs.get("http-equiv") or "").strip().lower()
            value = attrs.get("content", "").strip()
            if key and value:
                self.meta[key] = value
        elif tag.lower() == "link" and "canonical" in attrs.get("rel", "").lower().split():
            self.canonical = attrs.get("href", "").strip()
    def handle_endtag(self, tag):
        if tag.lower() == "title": self.in_title = False
    def handle_data(self, data):
        if self.in_title and data.strip(): self.title_parts.append(data.strip())
    @property
    def title(self): return " ".join(self.title_parts).strip()

def fetch(url, timeout=45, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except Exception as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(min(30, (2 ** attempt) + random.random()))
    raise last

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def allowed_url(url, root):
    u, r = urlparse(url), urlparse(root)
    # A root with a path restricts to that path; a domain root permits all paths
    # on that host. Scheme may differ in archived originals.
    same_host = u.netloc.lower() == r.netloc.lower()
    if not same_host:
        return False
    return not r.path.strip("/") or u.path.startswith(r.path)

def init_db(dbpath):
    con = sqlite3.connect(dbpath)
    con.execute("""CREATE TABLE IF NOT EXISTS pages (
      url TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
      attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS files (
      url TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'discovered',
      local_path TEXT, bytes INTEGER, sha256 TEXT, content_type TEXT,
      last_error TEXT, updated REAL)""")
    # Backwards-compatible provenance columns for databases created by earlier versions.
    cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    for name, typ in (("original_url", "TEXT"), ("capture_timestamp", "TEXT"), ("source_root", "TEXT")):
        if name not in cols: con.execute(f"ALTER TABLE files ADD COLUMN {name} {typ}")
    con.execute("""CREATE TABLE IF NOT EXISTS cdx_progress (
      root TEXT PRIMARY KEY, next_page INTEGER NOT NULL DEFAULT 0,
      total_pages INTEGER, status TEXT NOT NULL DEFAULT 'pending',
      last_error TEXT, updated REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS url_index (
      original_url TEXT NOT NULL, capture_timestamp TEXT NOT NULL DEFAULT '',
      status_code TEXT, mimetype TEXT, digest TEXT, length TEXT,
      source_root TEXT NOT NULL, replay_url TEXT, indexed_at REAL,
      PRIMARY KEY(original_url,capture_timestamp,source_root))""")
    con.execute("""CREATE TABLE IF NOT EXISTS page_metadata (
      replay_url TEXT PRIMARY KEY, original_url TEXT, capture_timestamp TEXT,
      source_root TEXT, status_code TEXT, mimetype TEXT, title TEXT,
      meta_description TEXT, meta_keywords TEXT, canonical_url TEXT,
      og_title TEXT, og_description TEXT, og_type TEXT, robots_meta TEXT,
      html_lang TEXT, meta_all_json TEXT, extracted_at REAL, last_error TEXT)""")
    meta_cols = {r[1] for r in con.execute("PRAGMA table_info(page_metadata)")}
    if "meta_all_json" not in meta_cols:
        con.execute("ALTER TABLE page_metadata ADD COLUMN meta_all_json TEXT")
    con.commit(); return con

def enqueue_page(con, url):
    con.execute("INSERT OR IGNORE INTO pages(url,status,updated) VALUES(?, 'pending', ?)", (url, time.time()))

def enqueue_file(con, url, source, original_url=None, timestamp=None, source_root=None):
    con.execute("""INSERT OR IGNORE INTO files(url,source,status,updated,original_url,capture_timestamp,source_root)
                   VALUES(?, ?, 'discovered', ?, ?, ?, ?)""",
                (url, source, time.time(), original_url, timestamp, source_root))

def live_crawl(con, root, delay):
    enqueue_page(con, root); con.commit()
    while True:
        row = con.execute("SELECT url FROM pages WHERE status='pending' ORDER BY url LIMIT 1").fetchone()
        if not row: break
        url = row[0]
        con.execute("UPDATE pages SET status='processing', attempts=attempts+1, updated=? WHERE url=?", (time.time(), url)); con.commit()
        try:
            data, _ = fetch(url)
            parser = LinkParser(); parser.feed(data.decode("utf-8", "replace"))
            for href in parser.links:
                if href.startswith(("#", "?", "mailto:", "javascript:")): continue
                target = urljoin(url, href); pu = urlparse(target)
                target = pu._replace(query="", fragment="").geturl()
                if not allowed_url(target, root): continue
                ext = Path(unquote(pu.path).lower()).suffix
                if ext in EXTENSIONS: enqueue_file(con, target, "live", original_url=target, source_root=root)
                elif pu.path.endswith("/"): enqueue_page(con, target)
            con.execute("UPDATE pages SET status='completed',last_error=NULL,updated=? WHERE url=?", (time.time(), url)); con.commit()
            print(f"[live page] {url}"); time.sleep(max(0, delay))
        except Exception as e:
            con.execute("UPDATE pages SET status='failed',last_error=?,updated=? WHERE url=?", (str(e), time.time(), url)); con.commit()
            print(f"[live page failed; resumable after resetting status] {url}: {e}", file=sys.stderr)

def cdx_json(params):
    query = urllib.parse.urlencode(params)
    raw, _ = fetch(CDX_URL + "?" + query, timeout=90)
    return json.loads(raw.decode("utf-8", "replace"))

def get_page_count(base_params):
    try:
        result = cdx_json({**base_params, "showNumPages": "true"})
        if isinstance(result, dict): return int(result.get("pages", 0))
        if isinstance(result, list) and result:
            item = result[0]
            if isinstance(item, list) and item: return int(item[0])
            return int(item)
        return int(str(result).strip())
    except Exception as exc:
        print(f"[CDX page count unavailable; will page until empty] {exc}", file=sys.stderr)
        return None

def parse_cdx_page(parsed):
    if not isinstance(parsed, list) or not parsed: return [], None
    # Standard JSON output is [header, ...rows]. Pagination/resume-key output
    # may append [], [resume_key]; strip those control records if present.
    header = parsed[0] if isinstance(parsed[0], list) else None
    if not header: return [], None
    rows = parsed[1:]; resume = None
    if len(rows) >= 2 and rows[-2] == [] and isinstance(rows[-1], list):
        resume = rows[-1][0] if rows[-1] else None
        rows = rows[:-2]
    clean = [r for r in rows if isinstance(r, list) and r]
    return [dict(zip(header, row)) for row in clean], resume

def wayback_inventory(con, roots, delay):
    for root in roots:
        parsed_root = urlparse(root)
        host = parsed_root.netloc
        # CDX wildcard indexes paths below the host. Scheme variants are covered
        # by the host wildcard; exact original URL checks preserve scope.
        pattern = host + "/*"
        base = {"url": pattern, "output": "json", "filter": "statuscode:200",
                "fl": "original,timestamp,statuscode,mimetype,digest,length", "collapse": "urlkey"}
        con.execute("INSERT OR IGNORE INTO cdx_progress(root,next_page,status,updated) VALUES(?,0,'pending',?)", (root, time.time())); con.commit()
        state = con.execute("SELECT next_page,total_pages,status FROM cdx_progress WHERE root=?", (root,)).fetchone()
        next_page, total_pages, status = state
        if status == "completed":
            print(f"[CDX already complete] {root}"); continue
        if total_pages is None:
            total_pages = get_page_count(base)
            con.execute("UPDATE cdx_progress SET total_pages=?,updated=? WHERE root=?", (total_pages, time.time(), root)); con.commit()
        page = int(next_page or 0); processed = 0
        while True:
            if total_pages is not None and page >= total_pages: break
            params = {**base, "page": page}
            try:
                parsed = cdx_json(params)
                rows, _ = parse_cdx_page(parsed)
            except Exception as exc:
                con.execute("UPDATE cdx_progress SET next_page=?,status='pending',last_error=?,updated=? WHERE root=?", (page, str(exc), time.time(), root)); con.commit()
                print(f"[CDX page error; resumable at page {page}] {root}: {exc}", file=sys.stderr)
                break
            if not rows:
                # If page count could not be read, empty page is the end. If a
                # known count says more pages exist, record the issue and stop.
                if total_pages is not None and page < total_pages:
                    con.execute("UPDATE cdx_progress SET next_page=?,status='pending',last_error=?,updated=? WHERE root=?", (page, "unexpected empty page before known end", time.time(), root)); con.commit()
                else:
                    con.execute("UPDATE cdx_progress SET status='completed',last_error=NULL,updated=? WHERE root=?", (time.time(), root)); con.commit()
                break
            for row in rows:
                original = row.get("original", "")
                # Host-wide wildcard may include both HTTP and HTTPS originals.
                if not original or urlparse(original).netloc.lower() != host.lower(): continue
                ext = Path(unquote(urlparse(original).path).lower()).suffix
                ts = row.get("timestamp", "")
                replay = f"https://web.archive.org/web/{ts}id_/{original}"
                mimetype = (row.get("mimetype") or "").lower()
                # Preserve every row from Wayback's paginated URL index, not only
                # files and HTML. This is the canonical inventory for later passes.
                con.execute("""INSERT OR IGNORE INTO url_index
                    (original_url,capture_timestamp,status_code,mimetype,digest,length,source_root,replay_url,indexed_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (original, ts, row.get("statuscode"), row.get("mimetype"), row.get("digest"),
                     row.get("length"), root, replay, time.time()))
                if ext in EXTENSIONS:
                    enqueue_file(con, replay, "wayback", original_url=original, timestamp=ts, source_root=root)
                    processed += 1
                # Keep every indexed URL/capture eligible for metadata extraction.
                if mimetype in ("text/html", "application/xhtml+xml") or ext in ("", ".html", ".htm", ".xhtml", ".php", ".asp", ".aspx"):
                    con.execute("""INSERT OR IGNORE INTO page_metadata
                        (replay_url,original_url,capture_timestamp,source_root,status_code,mimetype)
                        VALUES(?,?,?,?,?,?)""",
                        (replay, original, ts, root, row.get("statuscode"), row.get("mimetype")))
                # Record discovered URL/capture rows too, not just downloadable file types.
                con.execute("""INSERT OR IGNORE INTO pages(url,status,updated) VALUES(?, 'pending', ?)""",
                            (replay, time.time()))
            page += 1
            con.execute("UPDATE cdx_progress SET next_page=?,updated=? WHERE root=?", (page, time.time(), root)); con.commit()
            print(f"[CDX page {page}/{total_pages if total_pages is not None else '?'}] {root} — {len(rows)} index rows, {processed} matching files queued")
            time.sleep(max(0, delay))
        # Mark completed only when all known pages have been consumed.
        state = con.execute("SELECT next_page,total_pages,status FROM cdx_progress WHERE root=?", (root,)).fetchone()
        if state[1] is not None and state[0] >= state[1]:
            con.execute("UPDATE cdx_progress SET status='completed',last_error=NULL,updated=? WHERE root=?", (time.time(), root)); con.commit()

def download_files(con, out, delay, limit=0):
    rows = con.execute("SELECT url,source,original_url,capture_timestamp,source_root FROM files WHERE status IN ('discovered','failed') ORDER BY url").fetchall()
    if limit > 0: rows = rows[:limit]
    base = out / "recovered"
    for i, (url, source, original_url, timestamp, source_root) in enumerate(rows, 1):
        pu = urlparse(url); relparts = [p for p in unquote(pu.path).split("/") if p not in ("", ".", "..")]
        rel = Path(*relparts) if relparts else Path("unnamed")
        if source == "wayback" and timestamp: rel = Path(timestamp) / rel
        dest = base / source / rel; dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest.exists() and dest.stat().st_size:
                size, digest = dest.stat().st_size, sha256_file(dest); ctype = mimetypes.guess_type(str(dest))[0] or ""
            else:
                data, ctype = fetch(url, timeout=90)
                if not data: raise RuntimeError("empty response")
                dest.write_bytes(data); size, digest = len(data), sha256_file(dest)
            con.execute("""UPDATE files SET status='completed',local_path=?,bytes=?,sha256=?,content_type=?,last_error=NULL,updated=? WHERE url=?""", (str(dest), size, digest, ctype, time.time(), url)); con.commit()
            print(f"[file {i}/{len(rows)}] {original_url or url} -> {dest} ({size} bytes)")
        except Exception as e:
            con.execute("UPDATE files SET status='failed',last_error=?,updated=? WHERE url=?", (str(e), time.time(), url)); con.commit()
            print(f"[download failed] {url}: {e}", file=sys.stderr)
        time.sleep(max(0, delay))

def extract_metadata(con, delay, limit=0):
    rows = con.execute("""SELECT replay_url,original_url,capture_timestamp,source_root
        FROM page_metadata WHERE extracted_at IS NULL ORDER BY source_root,original_url,capture_timestamp""").fetchall()
    if limit > 0: rows = rows[:limit]
    for i, (replay, original, ts, root) in enumerate(rows, 1):
        try:
            data, ctype = fetch(replay, timeout=60)
            parser = MetaParser()
            parser.feed(data.decode("utf-8", "replace"))
            m = parser.meta
            con.execute("""UPDATE page_metadata SET title=?,meta_description=?,meta_keywords=?,
                canonical_url=?,og_title=?,og_description=?,og_type=?,robots_meta=?,html_lang=?,meta_all_json=?,
                extracted_at=?,last_error=NULL WHERE replay_url=?""",
                (parser.title, m.get("description",""), m.get("keywords",""), parser.canonical,
                 m.get("og:title",""), m.get("og:description",""), m.get("og:type",""),
                 m.get("robots",""), parser.html_lang, json.dumps(m, ensure_ascii=False, sort_keys=True),
                 time.time(), replay))
            con.commit()
            print(f"[meta {i}/{len(rows)}] {original} ({ts})")
        except Exception as exc:
            con.execute("UPDATE page_metadata SET last_error=? WHERE replay_url=?", (str(exc), replay))
            con.commit()
            print(f"[metadata failed] {replay}: {exc}", file=sys.stderr)
        time.sleep(max(0, delay))

def export_url_index_csv(con, path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["original_url", "capture_timestamp", "status_code", "mimetype", "digest", "length", "source_root", "replay_url", "indexed_at"])
        for row in con.execute("""SELECT original_url,capture_timestamp,status_code,mimetype,digest,length,source_root,replay_url,indexed_at
                                  FROM url_index ORDER BY source_root,original_url,capture_timestamp"""):
            w.writerow(row)

def export_metadata_csv(con, path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["replay_url","original_url","capture_timestamp","source_root","status_code","mimetype",
                    "title","meta_description","meta_keywords","canonical_url","og_title","og_description",
                    "og_type","robots_meta","html_lang","meta_all_json","extracted_at","last_error"])
        for row in con.execute("""SELECT replay_url,original_url,capture_timestamp,source_root,status_code,mimetype,
                    title,meta_description,meta_keywords,canonical_url,og_title,og_description,og_type,
                    robots_meta,html_lang,meta_all_json,extracted_at,last_error FROM page_metadata
                    ORDER BY source_root,original_url,capture_timestamp"""):
            w.writerow(row)

def export_csv(con, path):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["url", "source", "original_url", "capture_timestamp", "source_root", "status", "local_path", "bytes", "sha256", "content_type", "last_error"])
        for row in con.execute("SELECT url,source,original_url,capture_timestamp,source_root,status,local_path,bytes,sha256,content_type,last_error FROM files ORDER BY source_root,original_url,capture_timestamp,url"):
            w.writerow(row)

def main():
    ap = argparse.ArgumentParser(description="Resumable crawler using Wayback URL index/CDX pagination, plus file and HTML metadata indexing")
    ap.add_argument("--out", default="jedi-sec-recovery")
    ap.add_argument("--source", choices=["live", "wayback"], default="live")
    ap.add_argument("--root", action="append", help="root URL; repeat to add multiple sources")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests/pages")
    ap.add_argument("--limit", type=int, default=0, help="maximum files to download per run; 0=all")
    ap.add_argument("--meta-index", action="store_true", help="fetch archived HTML captures and index title/meta/canonical/Open Graph tags")
    args = ap.parse_args(); out = Path(args.out).resolve(); out.mkdir(parents=True, exist_ok=True)
    con = init_db(out / "recovery_state.sqlite3")
    try:
        if args.source == "live":
            for root in (args.root or [LIVE_ROOT]): live_crawl(con, root, args.delay)
        else:
            wayback_inventory(con, args.root or DEFAULT_WAYBACK_ROOTS, args.delay)
        if args.download: download_files(con, out, args.delay, args.limit)
        if args.meta_index: extract_metadata(con, args.delay, args.limit)
        export_csv(con, out / "inventory.csv")
        export_metadata_csv(con, out / "page_metadata.csv")
        export_url_index_csv(con, out / "wayback_url_index.csv")
        counts = dict(con.execute("SELECT status,COUNT(*) FROM files GROUP BY status").fetchall())
        print("File states:", json.dumps(counts, sort_keys=True))
        print("Inventory:", out / "inventory.csv")
        print("Metadata index:", out / "page_metadata.csv")
        print("Complete Wayback URL index:", out / "wayback_url_index.csv")
        print("URL-index rows:", con.execute("SELECT COUNT(*) FROM url_index").fetchone()[0])
        print("State DB:", out / "recovery_state.sqlite3")
        print("CDX progress:", json.dumps(con.execute("SELECT root,next_page,total_pages,status,last_error FROM cdx_progress").fetchall()))
        print("Safety: recovered files are not executed, extracted, or imported into a live database.")
    finally: con.close()

if __name__ == "__main__": main()
