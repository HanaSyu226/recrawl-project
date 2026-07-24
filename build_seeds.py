#!/usr/bin/env python3
"""
Build a larger seed URL list by sampling pages from each site's sitemap.

For every (homepage, category) pair in the input sites file, this:
  1. Locates the site's sitemap (via the Sitemap: directive in
     robots.txt, falling back to /sitemap.xml)
  2. Parses it, following sitemap index files that point to other
     sitemaps (one level of indirection, capped so a huge site can't
     make this run forever)
  3. Filters candidate URLs through robots.txt
  4. Randomly samples up to MAX_PER_SITE of the allowed URLs so no
     single domain dominates the dataset
  5. Appends everything to the output file as URL<TAB>category

Sites without a reachable or parseable sitemap are skipped with a
message, not a crash.

Usage:
    python build_seeds.py sites.txt
    python build_seeds.py sites.txt -o seeds.txt --seed 42
"""

import argparse
import random
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from change_tracker import USER_AGENT, robots_allows

REQUEST_TIMEOUT = 15
MAX_PER_SITE = 30
MAX_CHILD_SITEMAPS = 20    # cap on how many child sitemaps to fetch from an index
MAX_CANDIDATE_URLS = 2000  # stop collecting once a site has offered up this many candidates
SITE_DELAY = 1.5           # polite delay between sites


def strip_ns(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def fetch_xml(url):
    """Fetch and parse a URL as XML. Returns an ElementTree root, or None on any failure."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return ET.fromstring(resp.content)
    except ET.ParseError:
        return None


def find_sitemap_url(homepage):
    """Look for a Sitemap: directive in robots.txt; fall back to /sitemap.xml."""
    parsed = urlparse(homepage)
    base = f"{parsed.scheme}://{parsed.netloc}"

    try:
        resp = requests.get(
            f"{base}/robots.txt", headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    return line.split(":", 1)[1].strip()
    except requests.RequestException:
        pass

    return f"{base}/sitemap.xml"


def collect_urls_from_sitemap(sitemap_url, seen_sitemaps, depth=0):
    """Recursively collect page URLs from a sitemap, following sitemap index
    files up to a small depth. Returns a list of URLs (not yet robots-filtered)."""
    if depth > 3 or sitemap_url in seen_sitemaps or len(seen_sitemaps) >= MAX_CHILD_SITEMAPS:
        return []
    seen_sitemaps.add(sitemap_url)

    root = fetch_xml(sitemap_url)
    if root is None:
        return []

    tag = strip_ns(root.tag)
    urls = []

    if tag == "sitemapindex":
        for sitemap_el in root:
            if strip_ns(sitemap_el.tag) != "sitemap":
                continue
            loc = None
            for child in sitemap_el:
                if strip_ns(child.tag) == "loc" and child.text:
                    loc = child.text.strip()
                    break
            if not loc:
                continue
            urls.extend(collect_urls_from_sitemap(loc, seen_sitemaps, depth + 1))
            if len(urls) >= MAX_CANDIDATE_URLS:
                break
            time.sleep(0.3)  # small politeness gap between child sitemap fetches

    elif tag == "urlset":
        for url_el in root:
            if strip_ns(url_el.tag) != "url":
                continue
            for child in url_el:
                if strip_ns(child.tag) == "loc" and child.text:
                    urls.append(child.text.strip())
                    break
            if len(urls) >= MAX_CANDIDATE_URLS:
                break

    return urls


def sample_site(homepage, category, rng):
    """Discover, robots-filter, and sample up to MAX_PER_SITE URLs for one site."""
    sitemap_url = find_sitemap_url(homepage)
    candidates = collect_urls_from_sitemap(sitemap_url, seen_sitemaps=set())

    if not candidates:
        print(f"  SKIP  {homepage}  (no usable sitemap found at {sitemap_url})")
        return []

    candidates = list(dict.fromkeys(candidates))  # de-dupe, preserve order
    allowed = [u for u in candidates if robots_allows(u)]

    if not allowed:
        print(f"  SKIP  {homepage}  ({len(candidates)} URLs found, none allowed by robots.txt)")
        return []

    sample_size = min(MAX_PER_SITE, len(allowed))
    sampled = rng.sample(allowed, sample_size)

    print(
        f"  OK    {homepage}  ->  {sample_size} URLs  "
        f"({len(candidates)} found, {len(allowed)} robots-allowed)"
    )
    return [(u, category) for u in sampled]


def load_sites(path):
    sites = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                print(f"  WARN  skipping malformed line: {raw!r}")
                continue
            sites.append((parts[0].strip(), parts[1].strip()))
    return sites


def main():
    ap = argparse.ArgumentParser(description="Sample seed URLs from site sitemaps")
    ap.add_argument("sites_file", help="input file: homepage_url<TAB>category per line")
    ap.add_argument("-o", "--output", default="seeds.txt", help="output file (default: seeds.txt)")
    ap.add_argument("--seed", type=int, default=42, help="random seed, for reproducible sampling")
    args = ap.parse_args()

    try:
        sites = load_sites(args.sites_file)
    except FileNotFoundError:
        sys.exit(f"Error: sites file not found: {args.sites_file}")

    rng = random.Random(args.seed)
    print(f"Loaded {len(sites)} sites from {args.sites_file}\n")

    all_rows = []
    seen_urls = set()

    for homepage, category in sites:
        rows = sample_site(homepage, category, rng)
        for url, cat in rows:
            if url not in seen_urls:
                seen_urls.add(url)
                all_rows.append((url, cat))
        time.sleep(SITE_DELAY)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(f"# Generated by build_seeds.py on {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"# {len(all_rows)} URLs from {len(sites)} sites (seed={args.seed})\n#\n")
        for url, cat in all_rows:
            f.write(f"{url}\t{cat}\n")

    print(f"\nWrote {len(all_rows)} URLs to {args.output}")

    by_cat = {}
    for _, cat in all_rows:
        by_cat[cat] = by_cat.get(cat, 0) + 1
    print("\nBy category:")
    for cat, n in sorted(by_cat.items(), key=lambda x: -x[1]):
        print(f"  {cat:<28} {n}")


if __name__ == "__main__":
    main()
