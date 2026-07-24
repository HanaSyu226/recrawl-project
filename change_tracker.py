#!/usr/bin/env python3
"""
Longitudinal web page change tracker.

Fetches a seed set of URLs on a schedule, extracts main content,
and records content hashes so that page change events can be
reconstructed later.

Only hashes and metadata are stored -- not page copies. This keeps
storage small and avoids copyright issues.

Setup:
    pip install requests trafilatura

Usage:
    python change_tracker.py --init seeds.txt    # one-time: load seed URLs
    python change_tracker.py --run               # one collection pass
    python change_tracker.py --stats             # progress summary

Schedule --run daily via cron (Linux/Mac) or Task Scheduler (Windows).
Example cron entry for 3am daily:
    0 3 * * * cd /path/to/project && python change_tracker.py --run
"""

import argparse
import hashlib
import logging
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
import trafilatura

DB_PATH = "change_data.db"
USER_AGENT = "AcademicResearchCrawler/1.0 (Master's research project; contact: am.hana.mk@gmail.com)"
REQUEST_TIMEOUT = 20
DEFAULT_DELAY = 2.0          # seconds between requests to the same host
POLITE_JITTER = (0.5, 1.5)   # random extra delay range

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[logging.FileHandler("crawler.log"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pages (
            id            INTEGER PRIMARY KEY,
            url           TEXT UNIQUE NOT NULL,
            category      TEXT,
            first_seen    TEXT,
            last_fetched  TEXT,
            fetch_count   INTEGER DEFAULT 0,
            change_count  INTEGER DEFAULT 0,
            active        INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS observations (
            id            INTEGER PRIMARY KEY,
            page_id       INTEGER NOT NULL,
            fetched_at    TEXT NOT NULL,
            content_hash  TEXT,
            content_len   INTEGER,
            http_status   INTEGER,
            changed       INTEGER,
            error         TEXT,
            FOREIGN KEY (page_id) REFERENCES pages(id)
        );

        CREATE INDEX IF NOT EXISTS idx_obs_page ON observations(page_id);
        CREATE INDEX IF NOT EXISTS idx_obs_time ON observations(fetched_at);
    """)
    conn.commit()
    return conn


def load_seeds(conn, path):
    """Load seed URLs. Format per line:  <url>  [TAB]  [category]"""
    added = 0
    now = datetime.now(timezone.utc).isoformat()

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            url = parts[0].strip()
            category = parts[1].strip() if len(parts) > 1 else "uncategorised"
            try:
                conn.execute(
                    "INSERT INTO pages (url, category, first_seen) VALUES (?, ?, ?)",
                    (url, category, now),
                )
                added += 1
            except sqlite3.IntegrityError:
                pass  # already present

    conn.commit()
    log.info("Loaded %d new seed URLs from %s", added, path)


# ----------------------------------------------------------------------
# Politeness
# ----------------------------------------------------------------------

_robots_cache = {}


def robots_allows(url):
    """Check robots.txt. Fails open on error, but logs it."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    if base not in _robots_cache:
        rp = RobotFileParser()
        rp.set_url(f"{base}/robots.txt")
        try:
            rp.read()
            _robots_cache[base] = rp
        except Exception as e:
            log.warning("Could not read robots.txt for %s: %s", base, e)
            _robots_cache[base] = None

    rp = _robots_cache[base]
    if rp is None:
        return True
    return rp.can_fetch(USER_AGENT, url)


# ----------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------

def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_page(url):
    """
    Returns (hash, length, status, error).
    Hash is None when extraction fails or the fetch errors.
    """
    if not robots_allows(url):
        return None, None, None, "disallowed_by_robots"

    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        return None, None, None, f"request_error: {type(e).__name__}"

    if resp.status_code != 200:
        return None, None, resp.status_code, f"http_{resp.status_code}"

    # Extract main content, excluding boilerplate (nav, ads, footers).
    # This is important: without it, rotating ads register as content changes.
    text = trafilatura.extract(
        resp.text,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )

    if not text:
        return None, None, resp.status_code, "extraction_failed"

    return content_hash(text), len(text), resp.status_code, None


def last_hash(conn, page_id):
    row = conn.execute(
        """SELECT content_hash FROM observations
           WHERE page_id = ? AND content_hash IS NOT NULL
           ORDER BY fetched_at DESC LIMIT 1""",
        (page_id,),
    ).fetchone()
    return row[0] if row else None


# ----------------------------------------------------------------------
# Collection pass
# ----------------------------------------------------------------------

def run_pass(conn, limit=None):
    rows = conn.execute(
        "SELECT id, url FROM pages WHERE active = 1 ORDER BY last_fetched IS NOT NULL, last_fetched"
    ).fetchall()

    if limit:
        rows = rows[:limit]

    log.info("Starting collection pass over %d pages", len(rows))

    stats = {"fetched": 0, "changed": 0, "errors": 0}
    host_last_hit = {}

    for page_id, url in rows:
        # Per-host politeness delay
        host = urlparse(url).netloc
        elapsed = time.time() - host_last_hit.get(host, 0)
        wait = DEFAULT_DELAY - elapsed
        if wait > 0:
            time.sleep(wait)
        time.sleep(random.uniform(*POLITE_JITTER))
        host_last_hit[host] = time.time()

        now = datetime.now(timezone.utc).isoformat()
        h, length, status, error = fetch_page(url)

        previous = last_hash(conn, page_id)
        changed = None
        if h is not None and previous is not None:
            changed = int(h != previous)

        conn.execute(
            """INSERT INTO observations
               (page_id, fetched_at, content_hash, content_len, http_status, changed, error)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (page_id, now, h, length, status, changed, error),
        )

        conn.execute(
            """UPDATE pages
               SET last_fetched = ?,
                   fetch_count = fetch_count + 1,
                   change_count = change_count + ?
               WHERE id = ?""",
            (now, changed or 0, page_id),
        )

        stats["fetched"] += 1
        if changed:
            stats["changed"] += 1
        if error:
            stats["errors"] += 1
            log.debug("Error on %s: %s", url, error)

        if stats["fetched"] % 100 == 0:
            conn.commit()
            log.info("  ... %d pages processed", stats["fetched"])

    conn.commit()
    log.info(
        "Pass complete: %d fetched, %d changed, %d errors",
        stats["fetched"], stats["changed"], stats["errors"],
    )


# ----------------------------------------------------------------------
# Stats
# ----------------------------------------------------------------------

def show_stats(conn):
    total_pages = conn.execute("SELECT COUNT(*) FROM pages WHERE active = 1").fetchone()[0]
    total_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    total_changes = conn.execute("SELECT COUNT(*) FROM observations WHERE changed = 1").fetchone()[0]

    span = conn.execute(
        "SELECT MIN(fetched_at), MAX(fetched_at) FROM observations"
    ).fetchone()

    print(f"\n{'='*56}")
    print(f"  Pages tracked        : {total_pages}")
    print(f"  Total observations   : {total_obs}")
    print(f"  Changes detected     : {total_changes}")
    if span[0]:
        print(f"  Collection started   : {span[0][:10]}")
        print(f"  Most recent pass     : {span[1][:10]}")
    print(f"{'='*56}\n")

    print("  By category:")
    for cat, n, obs, ch in conn.execute("""
        SELECT p.category,
               COUNT(DISTINCT p.id),
               COUNT(o.id),
               SUM(CASE WHEN o.changed = 1 THEN 1 ELSE 0 END)
        FROM pages p LEFT JOIN observations o ON o.page_id = p.id
        WHERE p.active = 1
        GROUP BY p.category ORDER BY 4 DESC
    """):
        rate = (ch / obs * 100) if obs else 0
        print(f"    {cat:<24} {n:>5} pages  {ch or 0:>6} changes  ({rate:>5.1f}%)")

    print("\n  Most volatile pages:")
    for url, fc, cc in conn.execute("""
        SELECT url, fetch_count, change_count FROM pages
        WHERE fetch_count > 3 ORDER BY CAST(change_count AS REAL)/fetch_count DESC LIMIT 10
    """):
        rate = cc / fc * 100 if fc else 0
        print(f"    {rate:>5.1f}%  {url[:64]}")
    print()


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Longitudinal web change tracker")
    ap.add_argument("--init", metavar="SEEDFILE", help="load seed URLs from file")
    ap.add_argument("--run", action="store_true", help="run one collection pass")
    ap.add_argument("--stats", action="store_true", help="show collection statistics")
    ap.add_argument("--limit", type=int, help="cap pages per pass (for testing)")
    args = ap.parse_args()

    conn = init_db()

    if args.init:
        load_seeds(conn, args.init)
    elif args.run:
        run_pass(conn, limit=args.limit)
    elif args.stats:
        show_stats(conn)
    else:
        ap.print_help()

    conn.close()


if __name__ == "__main__":
    main()
