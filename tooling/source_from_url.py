#!/usr/bin/env python3
"""
Generate a sources.json entry from a URL.

Usage:
  python tooling/source_from_url.py <url> [-g group1,group2] [--compact]

Outputs to stdout a JSON object compatible with rssel's sources file:
  {"title": "...", "url": "...", "groups": ["..."]}

The script tries to detect the title from RSS/Atom feeds first; if the URL is
HTML, it will fall back to <meta property="og:title"> or <title>.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html import unescape
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


def http_get(url: str, timeout: int = 15) -> tuple[bytes, dict[str, str]]:
    req = Request(url, headers={"User-Agent": "rssel-tool/0.1"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        # Build a simplified headers dict with lowercase keys
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return data, headers


def try_parse_feed_title(raw: bytes) -> Optional[str]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    tag = root.tag.lower()
    # Atom
    if tag.endswith("feed"):
        t = root.find("{http://www.w3.org/2005/Atom}title") or root.find("title")
        if t is not None and (t.text or "").strip():
            return (t.text or "").strip()
    # RSS 2.0 or RSS-like
    if tag.endswith("rss") or tag.endswith("rdf") or tag.endswith("rss2") or tag.endswith("xml"):
        ch = root.find("channel")
        if ch is not None:
            t = ch.find("title")
            if t is not None and (t.text or "").strip():
                return (t.text or "").strip()
    # Generic try: first <title>
    t = root.find("title")
    if t is not None and (t.text or "").strip():
        return (t.text or "").strip()
    return None


def extract_html_title(html: str) -> Optional[str]:
    # Prefer Open Graph title
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        return unescape(m.group(1).strip())
    # Then standard <title>
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = unescape(m.group(1)).strip()
        # Collapse whitespace
        title = re.sub(r"\s+", " ", title)
        return title if title else None
    return None


def decode_bytes(data: bytes, headers: dict[str, str]) -> str:
    # Use charset from headers if present
    ctype = headers.get("content-type", "")
    m = re.search(r"charset=([A-Za-z0-9_\-]+)", ctype)
    enc = m.group(1) if m else None
    if enc:
        try:
            return data.decode(enc, errors="replace")
        except Exception:
            pass
    # Try utf-8 then latin-1 as last resort
    try:
        return data.decode("utf-8")
    except Exception:
        return data.decode("latin-1", errors="replace")


def parse_groups_arg(g: Optional[str]) -> list[str]:
    if not g:
        return []
    return [s for s in re.split(r"[,\s]+", g) if s]


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate rssel sources.json entry from URL")
    ap.add_argument("url", help="Feed or page URL")
    ap.add_argument("-g", "--groups", help="Comma/space separated groups", default=None)
    ap.add_argument("--compact", action="store_true", help="Compact JSON (no indentation)")
    ap.add_argument("--timeout", type=int, default=15, help="Network timeout in seconds (default 15)")
    args = ap.parse_args()

    try:
        raw, headers = http_get(args.url, timeout=args.timeout)
    except (URLError, HTTPError) as e:
        print(f"error: failed to fetch URL: {e}", file=sys.stderr)
        return 2

    title = try_parse_feed_title(raw)
    if title is None:
        html = decode_bytes(raw, headers)
        title = extract_html_title(html)

    obj = {
        "title": title,
        "url": args.url,
        "groups": parse_groups_arg(args.groups),
    }

    if args.compact:
        print(json.dumps(obj, ensure_ascii=False))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

