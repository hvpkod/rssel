#!/usr/bin/env python3
"""
Discover RSS/Atom feed URLs from an HTML page.

Usage:
  # Discover feeds from a single page URL
  python tooling/discover_feeds.py <page_url> [-g group1,group2] [-t tier] [--ndjson|--array] [--compact]

  # Discover feeds from multiple page URLs listed in a file
  python tooling/discover_feeds.py --file pages.txt [-g group1,group2] [-t tier] [--ndjson|--array] [--compact]

Outputs JSON objects compatible with rssel's sources.json entries:
  {"title": "...", "url": "https://.../feed.xml", "groups": ["..."], "tier": 3}

By default, multiple results are printed as NDJSON (one JSON object per line).
Use --array to print one JSON array instead.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def http_get(url: str, timeout: int = 15) -> bytes:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; rssel-tool/0.1)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


class LinkFeedParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base = base_url
        self.links: list[dict] = []
        self.page_title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "link":
            m = {k.lower(): v for (k, v) in attrs}
            rel = (m.get("rel") or "").lower()
            typ = (m.get("type") or "").lower()
            href = m.get("href")
            title = m.get("title")
            if not href:
                return
            # Match common feed types
            is_alt = "alternate" in rel
            is_feed_type = any(t in typ for t in ("rss", "atom", "application/rss+xml", "application/atom+xml", "application/xml", "text/xml"))
            # Also accept common patterns in href
            href_l = href.lower()
            looks_like_feed = href_l.endswith(".xml") or "rss" in href_l or "atom" in href_l or "feed" in href_l
            if is_alt and (is_feed_type or looks_like_feed):
                abs_url = urljoin(self.base, href)
                self.links.append({"href": abs_url, "title": title})
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            t = (self.page_title or "") + data
            self.page_title = t.strip()


def parse_urls_from_file(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print(f"error: cannot read file: {e}", file=sys.stderr)
        return []
    urls: list[str] = []
    for tok in re.split(r"\s+", text):
        tok = tok.strip()
        if not tok or tok.startswith("#"):
            continue
        if re.match(r"^https?://", tok, flags=re.IGNORECASE):
            urls.append(tok)
    return urls


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover RSS/Atom feeds from an HTML page")
    ap.add_argument("url", nargs="?", help="Web page URL to discover feeds from")
    ap.add_argument("-f", "--file", help="File with page URLs (one per line or whitespace separated)")
    ap.add_argument("-g", "--groups", help="Comma/space separated groups", default=None)
    ap.add_argument("-t", "--tier", type=int, help="Tier 1-5 (1=highest frequency, 5=lowest)")
    ap.add_argument("--ndjson", action="store_true", help="Output one JSON object per line (default for --file)")
    ap.add_argument("--array", action="store_true", help="Output a single JSON array")
    ap.add_argument("--compact", action="store_true", help="Compact JSON (no indentation)")
    ap.add_argument("--timeout", type=int, default=15, help="Network timeout in seconds (default 15)")
    args = ap.parse_args()

    # Determine tier value
    tier_val = None
    if args.tier is not None:
        try:
            tv = int(args.tier)
            if 1 <= tv <= 5:
                tier_val = tv
        except Exception:
            tier_val = None

    # Groups parsing
    groups: list[str] = []
    if args.groups:
        groups = [s for s in re.split(r"[,\s]+", args.groups) if s]

    # Collect page URLs
    pages: list[str] = []
    if args.file:
        pages = parse_urls_from_file(args.file)
    elif args.url:
        pages = [args.url]
    else:
        print("error: provide a URL or --file", file=sys.stderr)
        return 2

    results: list[dict] = []
    for page in pages:
        try:
            raw = http_get(page, timeout=args.timeout)
        except (HTTPError, URLError) as e:
            print(f"warn: failed to fetch page {page}: {e}", file=sys.stderr)
            continue
        try:
            html = raw.decode("utf-8", errors="replace")
        except Exception:
            html = raw.decode(errors="replace")
        parser = LinkFeedParser(base_url=page)
        try:
            parser.feed(html)
        except Exception:
            pass
        # Build entries
        for link in parser.links:
            href = link.get("href")
            title = link.get("title") or parser.page_title or href
            obj = {"title": unescape(title), "url": href, "groups": groups}
            if tier_val is not None:
                obj["tier"] = tier_val
            results.append(obj)

    if not results:
        # best-effort: try common guesses if no link-tag feeds found
        guesses = ("/feed", "/feed.xml", "/rss.xml", "/atom.xml")
        for page in pages:
            for suf in guesses:
                href = urljoin(page, suf)
                obj = {"title": href.rsplit("/", 1)[-1], "url": href, "groups": groups}
                if tier_val is not None:
                    obj["tier"] = tier_val
                results.append(obj)

    if args.array:
        print(json.dumps(results, ensure_ascii=False) if args.compact else json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for obj in results:
            print(json.dumps(obj, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
