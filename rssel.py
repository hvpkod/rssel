#!/usr/bin/env python3
import argparse
import time
import os
import sys
import sqlite3
import json
import shutil
import tempfile
import subprocess
import tarfile
import io
from datetime import datetime, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET
import textwrap
from html import unescape
from html.parser import HTMLParser
import re
import sys
from urllib.parse import urlparse


APP_NAME = "rssel"


# ---------------- Simple ANSI color helpers ---------------- #

def _style(text: str, *codes) -> str:
    seq = ";".join(str(c) for c in codes if c is not None)
    return f"\033[{seq}m{text}\033[0m" if seq else text

def _maybe(text: str, enable: bool, *codes) -> str:
    return _style(text, *codes) if enable else text


def normalize_part(part: str | None) -> str | None:
    if not part:
        return part
    return "link" if part.lower() in ("url", "link") else part


def _parse_groups_arg(group_val: str | None) -> list[str]:
    if not group_val:
        return []
    return [g.strip() for g in re.split(r"[,\s]+", group_val) if g.strip()]


def _parse_date_arg(s: str | None) -> int | None:
    """Parse a date/time argument into a local epoch seconds.
    Supports:
    - 'today' (00:00 today)
    - 'yesterday' (00:00 yesterday)
    - 'YYYY-MM-DD'
    - 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DDTHH:MM'
    """
    if not s:
        return None
    s = s.strip().lower()
    now = datetime.now()
    if s == "today":
        dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(dt.timestamp())
    if s == "yesterday":
        dt = (now.replace(hour=0, minute=0, second=0, microsecond=0))
        dt = dt.replace(day=dt.day)  # noop, clarity
        dt = dt - timedelta(days=1)
        return int(dt.timestamp())
    fmts = ["%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return int(dt.timestamp())
        except Exception:
            continue
    return None


def full_config_template() -> str:
    return (
        "# rssel config (self-contained)\n"
        "# Paths are relative to the project folder by default\n"
        "data_dir = \"./.rssel\"\n"
        "sources_file = \"./.rssel/sources.json\"\n"
        "stopwords_file = \"./.rssel/stopwords.txt\"\n\n"
        "# Sync auto-tagging defaults\n"
        "sync_auto_tags = \"true\"\n"
        "sync_max_tags = \"5\"\n"
        "sync_include_domain = \"false\"\n\n"
        "# Display defaults (\"true\"/\"false\")\n"
        "display_color = \"false\"\n"
        "display_show_url = \"false\"\n"
        "display_show_tags = \"false\"\n"
        "display_show_path = \"false\"\n"
        "display_show_date = \"false\"\n"
        "display_show_snippet = \"false\"\n"
        "display_snippet_len = \"240\"\n\n"
        "# Internal file-tree export (list/pick/pick-tags --export and sync)\n"
        "export_dir = \"./.rssel/fs\"\n"
        "export_format = \"md\"\n\n"
        "# External export defaults (export --to file)\n"
        "external_export_dir = \"./export\"\n"
        "external_export_format = \"md\"\n\n"
        "# list --new window (hours)\n"
        "new_hours = \"24\"\n\n"
        "# list default max items (used when --limit not set)\n"
        "list_max = \"2000\"\n\n"
        "# copy defaults\n"
        "# part: url|title|summary|content\n"
        "copy_default_part = \"url\"\n"
        "copy_default_plain = \"false\"\n"
        "# supports \\n and \\t escapes\n"
        "copy_separator = \"\\n\"\n\n"
        "# Tools\n"
        "# Preferred editor (RSSEL_EDITOR env overrides)\n"
        "editor = \"nvim\"\n"
        "# Clipboard command (auto-detects wl-copy/xclip/xsel/pbcopy if empty)\n"
        "clipboard_cmd = \"\"\n\n"
        "# Highlight\n"
        "# Path to a newline-separated word/phrase list for highlighting\n"
        "highlight_words_file = \"./.rssel/highlights.txt\"\n"
    )


def default_stopwords_content() -> str:
    # Load base stopwords from repository file to avoid large inline strings
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "stopwords.base.txt")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        # Minimal fallback to keep running
        return "# base stopwords\n" + "\n".join([
            "the","and","or","but","with","for","from","this","that","have","has","are","is","be","was","were",
            "you","your","our","they","their","it","its","on","in","to","of","as","at","by","about","after",
            "before","also","new","one","two","three"
        ]) + "\n"


def cfg_flag(cfg: dict, key: str, default: bool = False) -> bool:
    val = cfg.get(key)
    if val is None:
        return default
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "on")


def resolve_display_opts(args) -> dict:
    cfg = read_config()
    # CLI flags override; otherwise use config defaults
    def opt(name: str, default=False):
        return getattr(args, name, False) or cfg_flag(cfg, f"display_{name}", default)
    # Determine order of show fields based on CLI flag order
    def cli_show_order(argv: list[str]) -> list[str]:
        flag_map = {
            "--show-url": "url",
            "--show-tags": "tags",
            "--show-path": "path",
            "--show-date": "date",
            "--show-snippet": "snippet",
            "--show-source": "source",
        }
        order: list[str] = []
        for tok in argv:
            key = flag_map.get(tok)
            if key and key not in order:
                order.append(key)
        return order
    order = cli_show_order(sys.argv[1:]) or ["url", "tags", "path", "date", "snippet"]
    # Snippet length
    snip_str = cfg.get("display_snippet_len")
    try:
        snippet_len = int(snip_str) if snip_str is not None else 240
    except ValueError:
        snippet_len = 240
    # Color handling: enabled via --color or config; --nocolor disables
    color_enabled = opt("color")
    if getattr(args, "nocolor", False):
        color_enabled = False
    show_tags = False if getattr(args, "no_show_tags", False) else opt("show_tags")
    return {
        "show_url": opt("show_url"),
        "show_tags": show_tags,
        "show_path": opt("show_path"),
        "show_date": opt("show_date"),
        "show_snippet": opt("show_snippet"),
        "show_source": opt("show_source"),
        "color": color_enabled,
        "order": order,
        "snippet_len": snippet_len,
    }


def rssel_home() -> str:
    # Self-contained by default: use local .rssel directory unless RSSEL_HOME is set
    base = os.environ.get("RSSEL_HOME")
    if base:
        return os.path.abspath(base)
    return os.path.abspath(os.path.join(os.getcwd(), f".{APP_NAME}"))


def ensure_dirs():
    home = rssel_home()
    os.makedirs(home, exist_ok=True)
    return home


def paths():
    home = ensure_dirs()
    return {
        "home": home,
        "config": os.path.join(home, "config.toml"),
        "sources": os.path.join(home, "sources.json"),
        "db": os.path.join(home, "data.sqlite"),
        "stopwords": os.path.join(home, "stopwords.txt"),
        "highlights": os.path.join(home, "highlights.txt"),
    }


def load_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def save_file(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def detect_editor(config: dict) -> list[str]:
    # Priority: RSSEL_EDITOR, EDITOR, config.editor, fallback to nvim then vi
    cmd = os.environ.get("RSSEL_EDITOR") or os.environ.get("EDITOR") or config.get("editor")
    if cmd:
        return cmd.split()
    for c in ("nvim", "vim", "vi"):
        if shutil.which(c):
            return [c]
    return ["vi"]


def detect_clipboard_cmd(config: dict) -> list[str] | None:
    if config.get("clipboard_cmd"):
        return config["clipboard_cmd"].split()
    for c in ("wl-copy", "xclip", "xsel", "pbcopy"):
        if shutil.which(c):
            if c == "xclip":
                return ["xclip", "-selection", "clipboard"]
            if c == "xsel":
                return ["xsel", "--clipboard", "--input"]
            return [c]
    return None


# Note: legacy TOML sources support removed; JSON-only now.


def parse_sources_file(text: str) -> list[dict]:
    """Parse sources file supporting two forms:
    1) Legacy:
       [groups]\n
       group = ["url1","url2"]
       -> each url belongs to one or more groups
    2) New blocks:
       [[source]]\n
       title = "Name"\n
       url = "https://..."\n
       groups = ["g1","g2"]

    Returns list of dicts: {url, title, groups: [..]}
    """
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if isinstance(data, dict) and "sources" in data:
        data = data.get("sources")
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for obj in data:
        if not isinstance(obj, dict):
            continue
        url = obj.get("url")
        if not url:
            continue
        title = obj.get("title")
        groups = obj.get("groups") or []
        if not isinstance(groups, list):
            groups = []
        out.append({"url": url, "title": title, "groups": [g for g in groups if isinstance(g, str)]})
    return out


def read_config() -> dict:
    p = paths()
    cfg = {
        "data_dir": p["home"],
        "sources_file": p["sources"],
        "stopwords_file": p["stopwords"],
        "highlight_words_file": p["highlights"],
        "export_dir": os.path.join(rssel_home(), "fs"),
        "export_format": "md",
        "external_export_dir": None,
        "external_export_format": "md",
        "new_hours": "24",
        "list_max": "2000",
        "editor": None,
        "clipboard_cmd": None,
    }
    text = load_file(p["config"]) or ""
    # very small key = "value" parser; ignore non-quoted values for safety
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
            cfg[key] = val
    return cfg


def read_sources_entries() -> list[dict]:
    p = paths()
    text = load_file(p["sources"]) or ""
    return parse_sources_file(text)


def db_conn():
    p = paths()
    os.makedirs(os.path.dirname(p["db"]), exist_ok=True)
    conn = sqlite3.connect(p["db"]) 
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS feeds (
            url TEXT PRIMARY KEY,
            grp TEXT NOT NULL,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS feed_groups (
            url TEXT NOT NULL,
            grp TEXT NOT NULL,
            PRIMARY KEY(url, grp),
            FOREIGN KEY(url) REFERENCES feeds(url) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_url TEXT NOT NULL,
            guid TEXT,
            title TEXT,
            link TEXT,
            summary TEXT,
            content TEXT,
            published_ts INTEGER,
            read INTEGER DEFAULT 0,
            UNIQUE(feed_url, guid) ON CONFLICT IGNORE,
            FOREIGN KEY(feed_url) REFERENCES feeds(url) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_items_feed_ts ON items(feed_url, published_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_items_read ON items(read);
        -- Tagging schema
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS item_tags (
            item_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY(item_id, tag_id),
            FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE,
            FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );
        """
    )
    # Migrations: add columns lazily if missing
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(items)")
    cols = {r[1] for r in cur.fetchall()}  # r[1] is name
    if "starred" not in cols:
        cur.execute("ALTER TABLE items ADD COLUMN starred INTEGER DEFAULT 0")
    if "deleted" not in cols:
        cur.execute("ALTER TABLE items ADD COLUMN deleted INTEGER DEFAULT 0")
    if "created_ts" not in cols:
        cur.execute("ALTER TABLE items ADD COLUMN created_ts INTEGER DEFAULT 0")
    # Feeds migrations
    cur.execute("PRAGMA table_info(feeds)")
    fcols = {r[1] for r in cur.fetchall()}
    if "title" not in fcols:
        try:
            cur.execute("ALTER TABLE feeds ADD COLUMN title TEXT")
        except Exception:
            pass
    if "archived" not in fcols:
        try:
            cur.execute("ALTER TABLE feeds ADD COLUMN archived INTEGER DEFAULT 0")
        except Exception:
            pass
    conn.commit()
    # Helpful indexes for common queries
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_items_deleted ON items(deleted);
        CREATE INDEX IF NOT EXISTS idx_items_starred ON items(starred);
        CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_items_feed_deleted_read ON items(feed_url, deleted, read);
        CREATE INDEX IF NOT EXISTS idx_item_tags_tag ON item_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_item_tags_item ON item_tags(item_id);
        CREATE INDEX IF NOT EXISTS idx_feeds_archived ON feeds(archived);
        """
    )
    conn.commit()


def cmd_init(args):
    p = paths()
    ensure_dirs()
    if not os.path.exists(p["config"]):
        save_file(p["config"], full_config_template())
        print(f"Created {p['config']}")
    else:
        print(f"Exists {p['config']}")

    if not os.path.exists(p["sources"]):
        save_file(
            p["sources"],
            json.dumps(
                {
                    "sources": [
                        {
                            "title": "This Week in Rust",
                            "url": "https://this-week-in-rust.org/rss.xml",
                            "groups": ["tech"],
                        },
                        {
                            "title": "The Guardian World",
                            "url": "https://www.theguardian.com/world/rss",
                            "groups": ["news"],
                        },
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        print(f"Created {p['sources']}")
    else:
        print(f"Exists {p['sources']}")

    # Create a default stopwords file if missing
    if not os.path.exists(p["stopwords"]):
        save_file(p["stopwords"], default_stopwords_content())
        print(f"Created {p['stopwords']}")
    else:
        print(f"Exists {p['stopwords']}")

    # Create a default highlights file if missing
    if not os.path.exists(p["highlights"]):
        save_file(p["highlights"], default_highlights_content())
        print(f"Created {p['highlights']}")
    else:
        print(f"Exists {p['highlights']}")

    conn = db_conn()
    init_db(conn)
    conn.close()
    print("Database ready")


def cmd_sources(args):
    entries = read_sources_entries()
    if not entries:
        print("No sources configured. Run 'rssel init' to create example files.")
        return 1
    # Build group -> list of (title,url)
    mapping: dict[str, list[tuple[str|None,str]]] = {}
    for e in entries:
        url = e.get("url")
        title = e.get("title")
        groups = e.get("groups") or ["ungrouped"]
        for g in groups:
            mapping.setdefault(g, []).append((title, url))
    # Optionally enrich with DB info (archived flag and item counts)
    db_cur = None
    if getattr(args, "with_db", False):
        conn = db_conn()
        init_db(conn)
        db_cur = conn.cursor()
    for grp in sorted(mapping.keys()):
        items = mapping[grp]
        print(f"[{grp}] ({len(items)})")
        for (title, url) in items:
            arch = ""
            count_str = ""
            if db_cur is not None:
                try:
                    db_cur.execute("SELECT archived FROM feeds WHERE url = ?", (url,))
                    row = db_cur.fetchone()
                    if row and row[0]:
                        arch = " [archived]"
                    db_cur.execute("SELECT COUNT(*) FROM items WHERE feed_url = ? AND deleted = 0", (url,))
                    cnt = (db_cur.fetchone() or (0,))[0]
                    count_str = f" (db items: {cnt})"
                except Exception:
                    pass
            if title:
                print(f"  - {title}{arch} — {url}{count_str}")
            else:
                print(f"  - {url}{arch}{count_str}")
    # Optionally list DB-only sources (not present in config)
    if db_cur is not None and getattr(args, "include_db_only", False):
        try:
            cfg_urls = {u for (_, ulist) in mapping.items() for (_, u) in ulist}
            db_cur.execute("SELECT url, COALESCE(title, url) as name, archived FROM feeds ORDER BY name COLLATE NOCASE")
            rows = db_cur.fetchall()
            extras = [(name, url, archived) for (url, name, archived) in rows if url not in cfg_urls]
            if extras:
                print(f"[DB-only] ({len(extras)})")
                for (name, url, archived) in extras:
                    arch = " [archived]" if archived else ""
                    # Count items
                    db_cur.execute("SELECT COUNT(*) FROM items WHERE feed_url = ? AND deleted = 0", (url,))
                    cnt = (db_cur.fetchone() or (0,))[0]
                    print(f"  - {name}{arch} — {url} (db items: {cnt})")
        except Exception:
            pass
    return 0


def http_get(url: str, timeout: int = 15) -> bytes | None:
    try:
        req = Request(url, headers={"User-Agent": "rssel/0.1"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (URLError, HTTPError) as e:
        print(f"Fetch error {url}: {e}", file=sys.stderr)
        return None


def parse_datetime(text: str | None) -> int | None:
    if not text:
        return None
    # Try common formats
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",  # RFC 2822 (RSS pubDate)
        "%Y-%m-%dT%H:%M:%S%z",        # Atom with timezone
        "%Y-%m-%dT%H:%M:%SZ",         # Atom Zulu
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(text, f)
            return int(dt.timestamp())
        except Exception:
            continue
    return None


def text_or_none(el):
    return (el.text or "").strip() if el is not None else None


def parse_feed(feed_url: str, raw: bytes) -> list[dict]:
    # Try to detect Atom vs RSS
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    tag = root.tag.lower()
    items: list[dict] = []
    ns = {
        "content": "http://purl.org/rss/1.0/modules/content/",
        "atom": "http://www.w3.org/2005/Atom",
    }
    if tag.endswith("feed"):  # Atom
        for entry in root.findall("atom:entry", ns) or root.findall("entry"):
            title = text_or_none(entry.find("atom:title", ns) or entry.find("title"))
            link_el = entry.find("atom:link[@rel='alternate']", ns) or entry.find("atom:link", ns) or entry.find("link")
            href = link_el.get("href") if link_el is not None else text_or_none(link_el)
            guid = text_or_none(entry.find("atom:id", ns) or entry.find("id")) or href or title
            summary = text_or_none(entry.find("atom:summary", ns) or entry.find("summary"))
            content_el = entry.find("atom:content", ns) or entry.find("content")
            content = text_or_none(content_el)
            published = text_or_none(entry.find("atom:published", ns) or entry.find("published") or entry.find("atom:updated", ns) or entry.find("updated"))
            ts = parse_datetime(published)
            items.append({
                "feed_url": feed_url,
                "guid": guid,
                "title": title,
                "link": href,
                "summary": summary,
                "content": content,
                "published_ts": ts or 0,
            })
    else:  # RSS 2.0 (or similar)
        channel = root.find("channel")
        if channel is None:
            channel = root
        items_nodes = channel.findall("item")
        if not items_nodes:
            items_nodes = root.findall("item")
        for it in items_nodes:
            title = text_or_none(it.find("title"))
            link = text_or_none(it.find("link"))
            guid = text_or_none(it.find("guid")) or link or title
            summary = text_or_none(it.find("description"))
            content_el = it.find("{http://purl.org/rss/1.0/modules/content/}encoded")
            content = text_or_none(content_el) or summary
            pub = text_or_none(it.find("pubDate"))
            ts = parse_datetime(pub)
            items.append({
                "feed_url": feed_url,
                "guid": guid,
                "title": title,
                "link": link,
                "summary": summary,
                "content": content,
                "published_ts": ts or 0,
            })
    return items


def upsert_feeds(conn: sqlite3.Connection, entries: list[dict], only_group: str | None):
    cur = conn.cursor()
    if not entries:
        return
    for e in entries:
        url = e.get("url")
        if not url:
            continue
        title = e.get("title")
        groups = e.get("groups") or []
        if only_group and only_group not in groups:
            continue
        primary = groups[0] if groups else "ungrouped"
        cur.execute(
            "INSERT INTO feeds(url, grp, title) VALUES(?, ?, ?) ON CONFLICT(url) DO UPDATE SET grp=excluded.grp, title=COALESCE(excluded.title, feeds.title)",
            (url, primary, title),
        )
        for g in (groups or [primary]):
            if only_group and g != only_group:
                continue
            cur.execute("INSERT OR IGNORE INTO feed_groups(url, grp) VALUES(?, ?)", (url, g))
    conn.commit()


def cmd_fetch(args):
    entries = read_sources_entries()
    if not entries:
        print("No sources configured. Run 'rssel init'.", file=sys.stderr)
        return 1
    conn = db_conn()
    init_db(conn)
    upsert_feeds(conn, entries, args.group)
    cur = conn.cursor()
    if args.group:
        glist = _parse_groups_arg(args.group)
        placeholders = ",".join(["?"] * len(glist)) if glist else "?"
        sql = f"SELECT DISTINCT f.url FROM feeds f JOIN feed_groups g ON g.url = f.url WHERE g.grp IN ({placeholders}) AND f.archived = 0"
        cur.execute(sql, glist or [args.group])
    else:
        cur.execute("SELECT url FROM feeds WHERE archived = 0")
    feed_urls = [r[0] for r in cur.fetchall()]
    total_new = 0
    for url in feed_urls:
        # Count existing items for this feed before insert
        try:
            cur.execute("SELECT COUNT(*) FROM items WHERE feed_url = ?", (url,))
            before_count = (cur.fetchone() or (0,))[0]
        except Exception:
            before_count = None
        raw = http_get(url)
        if raw is None:
            continue
        items = parse_feed(url, raw)
        now_ts = int(datetime.now().timestamp())
        for it in items:
            cur.execute(
                """
                INSERT OR IGNORE INTO items(feed_url, guid, title, link, summary, content, published_ts, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    it["feed_url"],
                    it.get("guid"),
                    it.get("title"),
                    it.get("link"),
                    it.get("summary"),
                    it.get("content"),
                    int(it.get("published_ts") or now_ts),
                    now_ts,
                ),
            )
        conn.commit()
        # Compute new inserts via table counts to avoid total_changes quirks
        if before_count is not None:
            try:
                cur.execute("SELECT COUNT(*) FROM items WHERE feed_url = ?", (url,))
                after_count = (cur.fetchone() or (0,))[0]
                delta_new = max(0, after_count - before_count)
            except Exception:
                delta_new = 0
        else:
            delta_new = 0
        total_new += delta_new
        print(f"Fetched {url}: {len(items)} items, new {delta_new}")
    print(f"Done. Total new items inserted: {total_new}")
    return 0


def _print_meta_block(conn: sqlite3.Connection, item: dict, dt: str, opts: dict):
    # Better structured metadata block under each item.
    indent = "     "  # 5 spaces
    marker = "> "
    color = opts.get("color", False)

    def kv(label: str, value: str, kc: int | None, vc: int | None):
        key = _maybe(f"{label}:", color, kc)
        val = _maybe(value, color, vc) if value is not None else ""
        print(f"{indent}{marker}{key} {val}")

    order = opts.get("order") or ["url", "tags", "path", "date", "snippet"]
    for field in order:
        if field == "url" and opts.get("show_url"):
            kv("url", item.get("link") or "", 36, 36)  # aqua
        elif field == "tags" and opts.get("show_tags"):
            tags = get_item_tag_names(conn, item["id"]) if item else []
            kv("tags", ", ".join(tags), 33, 33)  # yellow
        elif field == "path" and opts.get("show_path"):
            kv("path", expected_item_path(item, fmt='md', dest=default_fs_dest()), 32, 32)  # green
        elif field == "date" and opts.get("show_date"):
            kv("date", dt, 35, 35)  # purple
        elif field == "snippet" and opts.get("show_snippet"):
            body_html = (item.get("content") or item.get("summary") or "") if item else ""
            snippet = html_to_text(body_html)
            snippet = snippet.strip().replace("\n", " ")
            maxlen = int(opts.get("snippet_len", 240))
            if len(snippet) > maxlen:
                snippet = snippet[:maxlen].rstrip() + "…"
            if snippet:
                kv("snippet", snippet, 90, 90)  # gray
        elif field == "source" and opts.get("show_source"):
            # Title or URL of the feed
            feed_url = item.get("feed_url") if item else None
            src_name = None
            if feed_url:
                try:
                    c = conn.cursor()
                    c.execute("SELECT COALESCE(title, url) FROM feeds WHERE url = ?", (feed_url,))
                    row = c.fetchone()
                    if row:
                        src_name = row[0]
                except Exception:
                    src_name = None
            src_name = src_name or feed_url or ""
            kv("source", src_name, 36, 36)


def cmd_list(args):
    t0 = time.perf_counter()
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    # Resolve display options early so sources summary can use color
    opts = resolve_display_opts(args)
    # If --sources or --list-sources, list sources summary
    if getattr(args, "sources", False) or getattr(args, "list_sources", False):
        # Sorting for the summary
        order_by = "name COLLATE NOCASE"
        if getattr(args, "sort_id_rev", False):
            order_by = "rowid ASC"
        elif getattr(args, "sort_id", False):
            order_by = "rowid DESC"
        elif getattr(args, "sort_group", False):
            order_by = "feeds.grp COLLATE NOCASE, name COLLATE NOCASE"
        elif getattr(args, "sort_name", False):
            order_by = "name COLLATE NOCASE"
        cur.execute(f"SELECT rowid, url, COALESCE(title, url) as name, archived FROM feeds ORDER BY {order_by}")
        feeds_rows = cur.fetchall()
        # Build rows with counts, last date, top tags
        data = []
        for (rid, url, name, archived) in feeds_rows:
            # Count items and last published date
            cur.execute("SELECT COUNT(*), MAX(published_ts) FROM items WHERE feed_url = ? AND deleted = 0", (url,))
            row = cur.fetchone() or (0, None)
            count = row[0] or 0
            last_ts = row[1]
            last = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M") if last_ts else "----"
            # Top tags
            cur.execute(
                """
                SELECT t.name, COUNT(*) as c
                FROM tags t
                JOIN item_tags it ON it.tag_id = t.id
                JOIN items ON items.id = it.item_id
                WHERE items.feed_url = ? AND items.deleted = 0
                GROUP BY t.name
                ORDER BY c DESC, t.name
                LIMIT 5
                """,
                (url,),
            )
            tag_list = [r[0] for r in cur.fetchall()]
            data.append((rid, name, url, archived, count, last, tag_list))
        # Pretty print as a simple table
        id_w = 4
        items_w = 6
        state_w = 9  # 'ARCHIVED' or 'ACTIVE'
        last_w = 16
        name_w = 32
        url_w = 42
        def trunc(s, w):
            s = str(s)
            return s if len(s) <= w else (s[: w - 1] + "…")
        header = f"{'ID':>{id_w}}  {'Items':>{items_w}}  {'Last':<{last_w}}  {'Name':<{name_w}}  {'URL':<{url_w}}  Tags"
        print(_maybe(header, opts.get('color'), 2))
        print("-" * len(header))
        for (rid, name, url, archived, count, last, tags) in data:
            id_s = _maybe(f"{rid:>{id_w}d}", opts.get('color'), 2)
            items_s = _maybe(f"{count:>{items_w}d}", opts.get('color'), 2)
            last_s = _maybe(f"{trunc(last, last_w):<{last_w}}", opts.get('color'), 2)
            # Name with optional colored [archived] suffix, keeping column width
            if archived:
                suffix = _maybe(" [archived]", opts.get('color'), 31)
                base_w = max(0, name_w - len(" [archived]"))
                name_base = f"{trunc(name, base_w):<{base_w}}"
                name_s = _maybe(name_base, opts.get('color'), 1) + suffix
            else:
                name_s = _maybe(f"{trunc(name, name_w):<{name_w}}", opts.get('color'), 1)
            url_s = _maybe(f"{trunc(url, url_w):<{url_w}}", opts.get('color'), 36)
            tags_s = ", ".join(_maybe(t, opts.get('color'), 33) for t in tags)
            print(f"{id_s}  {items_s}  {last_s}  {name_s}  {url_s}  [" + tags_s + "]")
        return 0
    # Optional: list all tags with counts
    if getattr(args, "list_tags", False):
        where = ["items.deleted = 0"]
        params: list = []
        glist = _parse_groups_arg(args.group)
        if glist:
            placeholders = ",".join(["?"] * len(glist))
            where.append(f"EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp IN ({placeholders}))")
            params.extend(glist)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        # Sorting and limiting for tags
        tsort = getattr(args, "tags_sort", None) or "count-desc"
        if tsort == "name":
            order_sql = "ORDER BY t.name COLLATE NOCASE"
        elif tsort == "count-asc":
            order_sql = "ORDER BY cnt ASC, t.name COLLATE NOCASE"
        else:
            order_sql = "ORDER BY cnt DESC, t.name COLLATE NOCASE"
        limit_sql = f"LIMIT {int(args.tags_top)}" if getattr(args, "tags_top", None) else ""
        sql = f"""
            SELECT t.name, COUNT(*) as cnt
            FROM tags t
            JOIN item_tags it ON t.id = it.tag_id
            JOIN items ON items.id = it.item_id
            JOIN feeds ON feeds.url = items.feed_url
            {where_sql}
            GROUP BY t.name
            {order_sql}
            {limit_sql}
        """
        cur.execute(sql, params)
        for (name, cnt) in cur.fetchall():
            print(f"{cnt} {name}")
        return 0
    where = []
    params: list = []
    # exclude deleted by default
    where.append("items.deleted = 0")
    glist = _parse_groups_arg(args.group)
    if glist:
        placeholders = ",".join(["?"] * len(glist))
        where.append(f"items.feed_url IN (SELECT url FROM feed_groups WHERE grp IN ({placeholders}))")
        params.extend(glist)
    # Filter by source (id or url)
    src = getattr(args, "source", None)
    if src:
        url_val = None
        try:
            rid = int(src)
            cur.execute("SELECT url FROM feeds WHERE rowid = ?", (rid,))
            row = cur.fetchone()
            if row:
                url_val = row[0]
        except Exception:
            url_val = None
        if not url_val:
            url_val = src
        where.append("items.feed_url = ?")
        params.append(url_val)
    # multi-tag intersection filter
    if getattr(args, "tags", None):
        # parse comma/space separated tags
        tag_list = [t.strip().lower() for t in re.split(r"[,\s]+", args.tags) if t.strip()]
        if tag_list:
            placeholders = ",".join(["?"] * len(tag_list))
            where.append(
                f"items.id IN ("
                f"  SELECT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id"
                f"  WHERE t.name IN ({placeholders})"
                f"  GROUP BY it.item_id HAVING COUNT(DISTINCT t.name) = {len(tag_list)}"
                f")"
            )
            params.extend(tag_list)
    if args.unread_only:
        where.append("items.read = 0")
    elif getattr(args, "read_only", False):
        where.append("items.read = 1")
    if getattr(args, "new", False):
        cfg = read_config()
        try:
            hours = int(cfg.get("new_hours", "24"))
        except Exception:
            hours = 24
        now_ts = int(datetime.now().timestamp())
        cutoff = now_ts - hours * 3600
        where.append("items.created_ts >= ?")
        params.append(cutoff)
    # Date filtering (published or created)
    ts_field = "items.published_ts" if getattr(args, "date_field", "published") == "published" else "items.created_ts"
    if getattr(args, "on", None):
        day_start = _parse_date_arg(args.on)
        if day_start is not None:
            day_end = day_start + 24*3600
            where.append(f"{ts_field} >= ? AND {ts_field} < ?")
            params.extend([day_start, day_end])
    else:
        if getattr(args, "since", None):
            since_ts = _parse_date_arg(args.since)
            if since_ts is not None:
                where.append(f"{ts_field} >= ?")
                params.append(since_ts)
        if getattr(args, "until", None):
            until_ts = _parse_date_arg(args.until)
            if until_ts is not None:
                where.append(f"{ts_field} < ?")
                params.append(until_ts)
    if args.query:
        where.append("(items.title LIKE ? OR items.summary LIKE ?)")
        q = f"%{args.query}%"
        params.extend([q, q])
    # read/star filters
    if getattr(args, "read_only", False) and not args.unread_only:
        where.append("items.read = 1")
    if getattr(args, "star_only", False):
        where.append("items.starred = 1")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    # Effective limit: CLI --limit or config list_max
    eff_limit = None
    try:
        eff_limit = int(args.limit) if args.limit else int(read_config().get("list_max", "2000"))
    except Exception:
        eff_limit = None
    limit_sql = f"LIMIT {eff_limit}" if eff_limit else ""
    # Sorting
    order_by = None
    if getattr(args, "sort_id_rev", False):
        order_by = "items.id ASC"
    elif getattr(args, "sort_id", False):
        order_by = "items.id DESC"
    elif getattr(args, "sort_name", False):
        order_by = "LOWER(COALESCE(items.title,'')) ASC, items.id ASC"
    elif getattr(args, "sort_group", False):
        order_by = f"feeds.grp COLLATE NOCASE ASC, {ts_field} DESC, items.id DESC"
    elif getattr(args, "sort_count", False):
        order_by = f"(SELECT COUNT(*) FROM item_tags it2 WHERE it2.item_id = items.id) DESC, {ts_field} DESC, items.id DESC"
    elif getattr(args, "sort_date_old", False):
        order_by = f"{ts_field} ASC, items.id ASC"
    else:  # default or --sort-date-new
        order_by = f"{ts_field} DESC, items.id DESC"
    sql = f"""
        SELECT items.id, items.published_ts, items.read, feeds.grp, items.title, items.feed_url
        FROM items JOIN feeds ON items.feed_url = feeds.url
        {where_sql}
        ORDER BY {order_by}
        {limit_sql}
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    # opts already computed above
    # Optional highlight support
    hl_terms: list[str] = []
    do_highlight = getattr(args, "highlight", False) or getattr(args, "highlight_only", False)
    if do_highlight:
        hl_terms = load_highlight_words()
    # Optional export
    if getattr(args, "export", False) and rows:
        cfg = read_config()
        dest = args.dest or cfg.get("export_dir") or os.path.join(rssel_home(), "fs")
        fmt = args.format or cfg.get("export_format") or "md"
        n = export_rows(conn, rows, os.path.abspath(dest), fmt)
        print(f"Exported {n} items to {os.path.abspath(dest)} (format: {fmt})")
    # If JSON output requested, build and print JSON array
    if getattr(args, "json", False):
        out = []
        cur_groups = conn.cursor()
        src_name_cache: dict[str, str] = {}
        for (iid, ts, read, grp, title, feed_url) in rows:
            # Highlight filter (optional)
            is_hl = False
            if do_highlight and hl_terms:
                item_for_hl = get_item(conn, iid)
                title_hl = (item_for_hl.get("title") if item_for_hl else title) or ""
                body_hl = html_to_text((item_for_hl.get("content") if item_for_hl else None) or (item_for_hl.get("summary") if item_for_hl else None) or "")
                low = (title_hl + "\n" + body_hl).lower()
                is_hl = False
                for term in hl_terms:
                    t = term.strip()
                    if not t:
                        continue
                    if re.fullmatch(r"[\w\-]+", t, flags=re.UNICODE):
                        if re.search(rf"\b{re.escape(t)}\b", low, flags=re.IGNORECASE):
                            is_hl = True
                            break
                    else:
                        if t.lower() in low:
                            is_hl = True
                            break
                if getattr(args, "highlight_only", False) and not is_hl:
                    continue
            # groups
            try:
                cur_groups.execute("SELECT grp FROM feed_groups WHERE url = ? ORDER BY grp", (feed_url,))
                groups = [r[0] for r in cur_groups.fetchall()]
            except Exception:
                groups = []
            if not groups:
                groups = [grp] if grp else []
            # source name
            src_name = src_name_cache.get(feed_url)
            if src_name is None:
                try:
                    c = conn.cursor()
                    c.execute("SELECT COALESCE(title, url) FROM feeds WHERE url = ?", (feed_url,))
                    row = c.fetchone()
                    src_name = row[0] if row else (feed_url or "")
                except Exception:
                    src_name = feed_url or ""
                src_name_cache[feed_url] = src_name
            # item details
            item = get_item(conn, iid) or {"id": iid, "title": title, "group": grp, "feed_url": feed_url}
            link = item.get("link")
            path = expected_item_path(item, fmt='md', dest=default_fs_dest())
            tags = get_item_tag_names(conn, iid)
            body_html = (item.get("content") or item.get("summary") or "")
            snippet = html_to_text(body_html)
            maxlen = int(opts.get("snippet_len", 240))
            if len(snippet) > maxlen:
                snippet = snippet[:maxlen].rstrip() + "…"
            dt_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else None
            # Decide which metadata to include based strictly on CLI show flags
            want_url = bool(getattr(args, "show_url", False))
            want_path = bool(getattr(args, "show_path", False))
            want_tags = bool(getattr(args, "show_tags", False))
            want_date = bool(getattr(args, "show_date", False))
            want_snip = bool(getattr(args, "show_snippet", False))
            want_src = bool(getattr(args, "show_source", False))

            # Build object (base fields are always included)
            obj = {
                "id": iid,
                "title": title or "",
                "read": bool(read),
                "published_ts": ts,
                "primary_group": grp,
                "groups": groups[:],
                "feed_url": feed_url,
                "highlight": bool(is_hl),
            }
            if want_date:
                obj["date"] = dt_str
            if want_src:
                obj["source"] = src_name
            if want_url:
                obj["link"] = link
            if want_path:
                obj["path"] = path
            if want_tags:
                obj["tags"] = tags[:]
            if want_snip:
                obj["snippet"] = snippet

            # JSON output intentionally does not inject ANSI color codes into fields;
            # downstream JSON tooling should receive plain strings. Use non-JSON modes
            # (e.g., grid/list with --color) for colored terminal output.
            out.append(obj)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    # Pre-prepare a cursor for fetching groups per feed
    cur_groups = conn.cursor()
    # Optional grid layout
    grid = getattr(args, "grid", False)
    def _ansi_strip(s: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", s)
    def _pad(s: str, w: int, right: bool = True) -> str:
        clean = _ansi_strip(s)
        extra = max(0, w - len(clean))
        return (s + " " * extra) if right else (" " * extra + s)
    def _truncate(s: str, w: int) -> str:
        if w <= 0:
            return ""
        clean = _ansi_strip(s)
        if len(clean) <= w:
            return _pad(s, w)
        # truncate plain text and append ellipsis
        return clean[: max(0, w - 1)] + "…"
    # Prepare grid settings
    if grid:
        col_id = 6
        col_marks = 2
        col_groups = 20
        # In grid mode, ignore generic --show-* flags by default to reduce clutter.
        # Users can explicitly choose metadata rows with --grid-meta.
        allowed_meta = {"date", "path", "url", "tags", "source", "snippet"}
        raw_meta = getattr(args, "grid_meta", None) or ""
        meta_rows = [s for s in re.split(r"[,\s]+", raw_meta) if s]
        meta_rows = [m for m in meta_rows if m in allowed_meta]
        source_cache: dict[str, str] = {}
    shown_count = 0
    for (iid, ts, read, grp, title, feed_url) in rows:
        # Evaluate highlight state if requested
        is_hl = False
        if do_highlight and hl_terms:
            item_for_hl = get_item(conn, iid)
            title_hl = (item_for_hl.get("title") if item_for_hl else title) or ""
            body_hl = html_to_text((item_for_hl.get("content") if item_for_hl else None) or (item_for_hl.get("summary") if item_for_hl else None) or "")
            low = (title_hl + "\n" + body_hl).lower()
            for term in hl_terms:
                t = term.strip()
                if not t:
                    continue
                if re.fullmatch(r"[\w\-]+", t, flags=re.UNICODE):
                    if re.search(rf"\b{re.escape(t)}\b", low, flags=re.IGNORECASE):
                        is_hl = True
                        break
                else:
                    if t.lower() in low:
                        is_hl = True
                        break
        if getattr(args, "highlight_only", False) and not is_hl:
            continue

        mark = " " if read else "*"
        hmark = "!" if is_hl else " "
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
        # Compute all groups for this feed (fallback to primary if none)
        groups = []
        try:
            cur_groups.execute("SELECT grp FROM feed_groups WHERE url = ? ORDER BY grp", (feed_url,))
            groups = [r[0] for r in cur_groups.fetchall()]
        except Exception:
            groups = []
        if not groups:
            groups = [grp] if grp else []
        grp_label = "[" + ",".join(groups) + "]"
        # Build base strings
        id_s = _maybe(str(iid), opts["color"], 2)
        mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
        hmark_s = _maybe(hmark, opts["color"] and hmark == "!", 31, 1) if hmark.strip() else hmark
        marks = mark_s + hmark_s
        grp_s = _maybe(grp_label, opts["color"], 36)
        # Highlighted items: color title differently (magenta + bold)
        if is_hl:
            title_s = _maybe(title or '', opts["color"], 35, 1)
        else:
            title_s = _maybe(title or '', opts["color"], 1)
        dt_s = _maybe(dt, opts["color"], 2)

        if grid:
            # Base row: id, marks, groups, title (no truncation; piping friendly)
            print(_pad(id_s, col_id, right=False), _pad(marks, col_marks), _truncate(grp_s, col_groups), title_s)
            # Extra rows: id + label + value
            label_w = 10
            item = None
            for key in meta_rows:
                label = key
                if key == "date":
                    val = _maybe(dt, opts["color"], 2)
                elif key == "path":
                    if item is None:
                        item = get_item(conn, iid)
                    p = expected_item_path(item or {"id": iid, "title": title, "group": grp}, fmt='md', dest=default_fs_dest())
                    val = _maybe(p, opts["color"], 32)
                elif key == "url":
                    if item is None:
                        item = get_item(conn, iid)
                    val = (item or {}).get("link") or ""
                elif key == "tags":
                    tags = get_item_tag_names(conn, iid)
                    val = _maybe(", ".join(tags), opts["color"], 33)
                elif key == "source":
                    name = source_cache.get(feed_url)
                    if name is None:
                        try:
                            c2 = conn.cursor()
                            c2.execute("SELECT COALESCE(title, url) FROM feeds WHERE url = ?", (feed_url,))
                            row = c2.fetchone()
                            name = row[0] if row else (feed_url or "")
                        except Exception:
                            name = feed_url or ""
                        source_cache[feed_url] = name
                    val = _maybe(name, opts["color"], 36)
                elif key == "snippet":
                    if item is None:
                        item = get_item(conn, iid)
                    txt = html_to_text((item or {}).get("content") or (item or {}).get("summary") or "")
                    maxlen = int(opts.get("snippet_len", 240))
                    if len(txt) > maxlen:
                        txt = txt[:maxlen].rstrip() + "…"
                    val = _maybe(txt, opts["color"], 90, 90)
                else:
                    val = ""
                lab_s = _maybe(f"{label}:", opts["color"], 2)
                print(_pad(id_s, col_id, right=False), " ", _pad(lab_s, label_w), val)
            shown_count += 1
        else:
            if opts["show_url"] or opts["show_tags"] or opts["show_path"] or opts["show_snippet"] or opts["show_date"]:
                print(f"{id_s} {marks} {grp_s} {title_s}")
            else:
                id_p = _maybe(f"{iid:6d}", opts["color"], 2)
                grp_p = _maybe(grp_label, opts["color"], 36)
                print(f"{id_p} {marks} {grp_p} {dt_s}  {title_s}")
            shown_count += 1
            if opts.get("show_url") or opts.get("show_tags") or opts.get("show_path") or opts.get("show_snippet") or opts.get("show_date") or opts.get("show_source") or getattr(args, "show_source", False):
                # pass feed_url in fallback so source lookup works
                item = get_item(conn, iid) or {"id": iid, "link": None, "group": grp, "title": title, "feed_url": feed_url}
                # Merge show_source flag from args into opts for order handling
                if getattr(args, "show_source", False):
                    opts = dict(opts)
                    opts["show_source"] = True
                _print_meta_block(conn, item, dt, opts)
    # Summary line: number of items and elapsed time
    t1 = time.perf_counter()
    elapsed = t1 - t0
    print(f"Total: {shown_count} item(s)  in {elapsed:.2f}s")
    return 0


def get_item(conn: sqlite3.Connection, item_id: int):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT items.id, items.title, items.summary, items.content, items.link,
               items.published_ts, feeds.grp, items.feed_url
        FROM items JOIN feeds ON items.feed_url = feeds.url
        WHERE items.id = ?
        """,
        (item_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["id", "title", "summary", "content", "link", "published_ts", "group", "feed_url"]
    return dict(zip(keys, row))


def export_to_editor(text: str, config: dict):
    editor = detect_editor(config)
    with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".md", encoding="utf-8") as tf:
        tf.write(text)
        tf.flush()
        path = tf.name
    try:
        subprocess.run(editor + [path])
    finally:
        os.unlink(path)


def export_to_clipboard(text: str, config: dict):
    cmd = detect_clipboard_cmd(config)
    if not cmd:
        print("No clipboard tool found. Set clipboard_cmd in config.", file=sys.stderr)
        return 1
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    proc.communicate(input=text.encode("utf-8"))
    return 0


def cmd_export(args):
    conn = db_conn()
    init_db(conn)
    cfg = read_config()
    dest = args.to
    if dest in ("stdout", "editor", "clipboard") and len(args.ids) > 1:
        print("Export to stdout/editor/clipboard supports one id at a time. Use --to file for multiple.", file=sys.stderr)
        return 1
    if dest == "file":
        # Write one file per id to dest directory in chosen format
        out_dir = os.path.abspath(args.dest or cfg.get("external_export_dir") or os.getcwd())
        fmt = args.format or cfg.get("external_export_format") or "md"
        rows = []
        cur = conn.cursor()
        placeholders = ",".join(["?"] * len(args.ids))
        cur.execute(
            f"SELECT items.id, items.published_ts, items.read, feeds.grp, items.title FROM items JOIN feeds ON items.feed_url = feeds.url WHERE items.id IN ({placeholders})",
            list(args.ids),
        )
        rows = cur.fetchall()
        n = export_rows(conn, rows, out_dir, fmt)
        print(f"Exported {n} items to {out_dir} (format: {fmt})")
        return 0
    else:
        iid = args.ids[0]
        item = get_item(conn, iid)
        if not item:
            print(f"No item with id {iid}", file=sys.stderr)
            return 1
        part = normalize_part(args.part or "content")
        data = item.get(part) if part in ("title", "summary", "content", "link") else None
        if data is None:
            # Fallback: compose from available fields
            data = f"{item.get('title','')}\n\n{item.get('summary','')}\n\n{item.get('content','')}\n\n{item.get('link','')}\n"
        # Optional plain-text conversion
        if args.plain and part in ("summary", "content"):
            data = html_to_text(data)
        if dest == "stdout":
            print(data)
            return 0
        elif dest == "editor":
            return export_to_editor(data, cfg)
        elif dest == "clipboard":
            return export_to_clipboard(data, cfg)
        else:
            print(f"Unknown destination: {dest}", file=sys.stderr)
            return 1


def cmd_mark(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    val = 0 if args.state == "unread" else 1
    cur.execute("UPDATE items SET read = ? WHERE id = ?", (val, args.id))
    if cur.rowcount == 0:
        print(f"No item with id {args.id}", file=sys.stderr)
        return 1
    conn.commit()
    print(f"Marked {args.id} as {args.state}")
    return 0


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._block_tags = {"p", "div", "section", "article", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
        self._skip_depth = 0  # for <script>/<style>

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if tag in ("br",):
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if tag in self._block_tags:
            self._chunks.append("\n\n")

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if data:
            self._chunks.append(data)

    def get_text(self):
        text = unescape("".join(self._chunks))
        # Collapse excessive blank lines
        lines = [ln.rstrip() for ln in text.splitlines()]
        out: list[str] = []
        blank = 0
        for ln in lines:
            if ln.strip():
                out.append(ln)
                blank = 0
            else:
                if blank < 1:
                    out.append("")
                blank += 1
        return "\n".join(out).strip()


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    p = _HTMLTextExtractor()
    try:
        p.feed(html)
        return p.get_text()
    except Exception:
        # Robust fallback: strip tags and scripts/styles
        tmp = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html or "")
        tmp = re.sub(r"(?s)<[^>]+>", " ", tmp)
        tmp = unescape(tmp)
        tmp = re.sub(r"\s+", " ", tmp)
        return tmp.strip()


def detect_pager() -> list[str]:
    env = os.environ.get("RSSEL_PAGER") or os.environ.get("PAGER")
    if env:
        return env.split()
    # Prefer bat/batcat if available for nicer viewing
    for c in ("bat", "batcat"):
        if shutil.which(c):
            return [c, "--paging=always", "-p"]
    # Fallbacks
    if shutil.which("less"):
        return ["less", "-R"]
    if shutil.which("more"):
        return ["more"]
    return ["cat"]


def run_pager(text: str):
    pager = detect_pager()
    try:
        proc = subprocess.Popen(pager, stdin=subprocess.PIPE)
        proc.communicate(input=text.encode("utf-8", errors="replace"))
    except FileNotFoundError:
        print(text)


def format_item_for_reading(item: dict, mode: str = "plain", width: int | None = None) -> str:
    # Header
    dt = datetime.fromtimestamp(item.get("published_ts") or 0).strftime("%Y-%m-%d %H:%M") if item.get("published_ts") else ""
    header = [
        f"{item.get('title') or ''}",
        f"[{item.get('group') or ''}] {dt}",
        f"{item.get('link') or ''}",
        "",
    ]
    # Body selection
    body_html = item.get("content") or item.get("summary") or ""
    if mode == "raw":
        body = body_html
    else:
        body = html_to_text(body_html)
        # Wrapping
        if width is None:
            try:
                width = shutil.get_terminal_size((80, 20)).columns
            except Exception:
                width = 80
        wrapped: list[str] = []
        for para in body.split("\n\n"):
            para = para.strip()
            if not para:
                wrapped.append("")
            else:
                wrapped.append(textwrap.fill(para, width=width))
        body = "\n\n".join(wrapped)
    return "\n".join(header) + body + "\n"


# ---------------- Tagging ---------------- #

_STOPWORDS = {
    # common English stopwords (small set to avoid dependencies)
    "the","and","for","that","with","this","from","have","not","are","was","were","but","you","your","our",
    "has","had","any","can","all","out","his","her","its","who","what","when","where","why","how","into","over",
    "use","used","using","been","more","most","other","some","such","than","then","them","they","their","there",
    "in","on","at","to","of","by","as","it","is","be","a","an","or","we","i","he","she","my","me","up",
    "about","after","before","between","during","per","via","also","new","one","two","three","no","yes","if","else",
}


def load_stopwords() -> set[str]:
    cfg = read_config()
    path = cfg.get("stopwords_file")
    words: set[str] = set(_STOPWORDS)
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip().lower()
                    if not line or line.startswith("#"):
                        continue
                    # allow space-separated words per line as a convenience
                    for w in re.split(r"\s+", line):
                        w = w.strip("-_")
                        if w:
                            words.add(w)
        except Exception:
            pass
    return words


def default_highlights_content() -> str:
    return "\n".join([
        "# Highlight words/phrases (one per line)",
        "# Matching is case-insensitive and uses word boundaries where possible.",
        "# Examples:",
        "rust",
        "release notes",
        "sverige",
        "översikt",
        "källor",
        "",
    ])


def load_highlight_words() -> list[str]:
    cfg = read_config()
    path = cfg.get("highlight_words_file")
    words: list[str] = []
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    words.append(line)
        except Exception:
            pass
    return words


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    # Unicode-aware: keep letters/numbers/underscore and hyphens; split on others
    # \w matches Unicode word chars when using Python's re (default is UNICODE)
    words = re.split(r"[^\w\-]+", text)
    # Strip surrounding hyphens/underscores and drop pure-digit tokens
    return [w.strip("-_") for w in words if w and not w.isdigit()]


def extract_auto_tags(title: str | None, body_text: str | None, max_tags: int = 5, min_len: int = 3, stopwords: set[str] | None = None) -> list[str]:
    title = title or ""
    body = body_text or ""
    stop = stopwords if stopwords is not None else load_stopwords()
    weights: dict[str, int] = {}
    for w in _tokenize(body):
        if len(w) < min_len or w in stop:
            continue
        weights[w] = weights.get(w, 0) + 1
    # Title words get higher weight
    for w in _tokenize(title):
        if len(w) < min_len or w in stop:
            continue
        weights[w] = weights.get(w, 0) + 2
    # Sort by weight desc, then alpha
    ranked = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    tags = [w for (w, _) in ranked[:max_tags]]
    return tags


def upsert_tag(conn: sqlite3.Connection, name: str) -> int | None:
    name = name.strip().lower()
    if not name:
        return None
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))
    cur.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def replace_item_tags(conn: sqlite3.Connection, item_id: int, tag_names: list[str]):
    cur = conn.cursor()
    cur.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
    for name in tag_names:
        tid = upsert_tag(conn, name)
        if tid is not None:
            cur.execute("INSERT OR IGNORE INTO item_tags(item_id, tag_id) VALUES(?, ?)", (item_id, tid))
    conn.commit()


def get_item_tag_names(conn: sqlite3.Connection, item_id: int) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.name
        FROM tags t JOIN item_tags it ON t.id = it.tag_id
        WHERE it.item_id = ?
        ORDER BY t.name
        """,
        (item_id,),
    )
    return [r[0] for r in cur.fetchall()]


def next_unread_item(conn: sqlite3.Connection, group: str | None):
    cur = conn.cursor()
    if group:
        cur.execute(
            """
            SELECT items.id
            FROM items JOIN feeds ON items.feed_url = feeds.url
            WHERE items.read = 0 AND EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp = ?)
            ORDER BY items.published_ts DESC, items.id DESC
            LIMIT 1
            """,
            (group,),
        )
    else:
        cur.execute(
            """
            SELECT id FROM items
            WHERE read = 0
            ORDER BY published_ts DESC, id DESC
            LIMIT 1
            """
        )
    row = cur.fetchone()
    return row[0] if row else None


def cmd_view(args):
    conn = db_conn()
    init_db(conn)
    item_id = args.id
    if item_id is None and args.next:
        item_id = next_unread_item(conn, args.group)
        if item_id is None:
            print("No unread items.")
            return 0
    if item_id is None:
        print("Provide an id or --next.", file=sys.stderr)
        return 1
    item = get_item(conn, item_id)
    if not item:
        print(f"No item with id {item_id}", file=sys.stderr)
        return 1
    text = format_item_for_reading(item, mode=("raw" if args.raw else "plain"))
    run_pager(text)
    # Default: mark as read unless explicitly disabled
    do_mark = True
    if hasattr(args, "no_mark_read") and args.no_mark_read:
        do_mark = False
    if hasattr(args, "mark_read") and args.mark_read:
        do_mark = True
    if do_mark:
        cur = conn.cursor()
        cur.execute("UPDATE items SET read = 1 WHERE id = ?", (item_id,))
        conn.commit()
    return 0


def cmd_preview(args):
    conn = db_conn()
    init_db(conn)
    item = get_item(conn, args.id)
    if not item:
        print("Not found")
        return 1
    width = args.width
    if width is None:
        # Respect FZF_PREVIEW_COLUMNS if set
        try:
            width = int(os.environ.get("FZF_PREVIEW_COLUMNS", "0")) or None
        except ValueError:
            width = None
    text = format_item_for_reading(item, mode="plain", width=width)
    print(text)
    return 0


def detect_browser_opener() -> list[str] | None:
    for c in ("xdg-open", "open", "wslview"):
        if shutil.which(c):
            return [c]
    # Windows PowerShell fallback
    if os.name == "nt":
        return ["powershell", "Start-Process"]
    return None


def cmd_open(args):
    conn = db_conn()
    init_db(conn)
    opener = detect_browser_opener()
    if not opener:
        print("No system opener found (xdg-open/open).", file=sys.stderr)
        return 1
    opened = 0
    missing = []
    for iid in args.ids:
        item = get_item(conn, iid)
        if not item:
            missing.append(iid)
            continue
        link = item.get("link")
        if not link:
            print(f"Item {iid} has no link", file=sys.stderr)
            continue
        try:
            subprocess.Popen(opener + [link])
            opened += 1
        except Exception as e:
            print(f"Failed to open id {iid}: {e}", file=sys.stderr)
    if args.mark_read and opened:
        cur = conn.cursor()
        cur.executemany("UPDATE items SET read = 1 WHERE id = ?", [(iid,) for iid in args.ids])
        conn.commit()
    if missing:
        print("Missing ids: " + ", ".join(str(i) for i in missing), file=sys.stderr)
    return 0


def cmd_edit(args):
    conn = db_conn()
    init_db(conn)
    item = get_item(conn, args.id)
    if not item:
        print(f"No item with id {args.id}", file=sys.stderr)
        return 1
    part = normalize_part(args.part)
    data = item.get(part) if part in ("title", "summary", "content", "link") else None
    if data is None:
        data = f"{item.get('title','')}\n\n{item.get('summary','')}\n\n{item.get('content','')}\n\n{item.get('link','')}\n"
    if (args.plain or part in ("content", "summary")) and part in ("content", "summary"):
        data = html_to_text(data)
    return export_to_editor(data, read_config())


def cmd_config_template(args):
    p = paths()
    content = full_config_template()
    if args.write:
        save_file(p["config"], content)
        # Ensure stopwords file exists as referenced (write base list)
        if not os.path.exists(p["stopwords"]):
            save_file(p["stopwords"], default_stopwords_content())
        print(f"Wrote full config to {p['config']}")
        return 0
    else:
        print(content)
        return 0

def cmd_archive_id(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    if args.undo:
        cur.execute("UPDATE items SET deleted = 0 WHERE id = ?", (args.id,))
    else:
        # Refuse to archive starred items
        cur.execute("SELECT starred FROM items WHERE id = ?", (args.id,))
        row = cur.fetchone()
        if not row:
            print(f"No item {args.id}")
            return 1
        if row[0]:
            print("Cannot archive a starred item. Unstar it first.", file=sys.stderr)
            return 1
        cur.execute("UPDATE items SET deleted = 1 WHERE id = ?", (args.id,))
    conn.commit()
    print(("Unarchived" if args.undo else "Archived") + f" item {args.id}")
    return 0


def cmd_archive_source(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    # Resolve URL (supports --url or --id)
    url = getattr(args, "url", None)
    if not url and getattr(args, "id", None) is not None:
        try:
            cur.execute("SELECT url FROM feeds WHERE rowid = ?", (int(args.id),))
            row = cur.fetchone()
            url = row[0] if row else None
        except Exception:
            url = None
    if not url:
        print("Provide --url or a valid --id", file=sys.stderr)
        return 1
    if args.undo:
        cur.execute("UPDATE feeds SET archived = 0 WHERE url = ?", (url,))
        print(f"Unarchived source {url}")
    else:
        cur.execute("UPDATE feeds SET archived = 1 WHERE url = ?", (url,))
        print(f"Archived source {url}")
        if args.delete_items:
            cur.execute("UPDATE items SET deleted = 1 WHERE feed_url = ? AND starred = 0", (url,))
            print("  Marked existing items deleted")
    conn.commit()
    return 0


def cmd_archive_group(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    # Find all urls in the group
    cur.execute("SELECT url FROM feed_groups WHERE grp = ?", (args.name,))
    urls = [r[0] for r in cur.fetchall()]
    if not urls:
        print("No sources in that group")
        return 0
    if args.undo:
        cur.executemany("UPDATE feeds SET archived = 0 WHERE url = ?", [(u,) for u in urls])
        print(f"Unarchived {len(urls)} source(s) in group {args.name}")
    else:
        cur.executemany("UPDATE feeds SET archived = 1 WHERE url = ?", [(u,) for u in urls])
        print(f"Archived {len(urls)} source(s) in group {args.name}")
        if args.delete_items:
            cur.executemany("UPDATE items SET deleted = 1 WHERE feed_url = ? AND starred = 0", [(u,) for u in urls])
            print("  Marked existing items deleted")
    conn.commit()
    return 0


# ---------------- Delete commands (soft delete separate from archive) ---------------- #

def cmd_delete_id(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    if getattr(args, "undo", False):
        cur.execute("UPDATE items SET deleted = 0 WHERE id = ?", (args.id,))
        conn.commit()
        print(f"Undeleted item {args.id}")
        return 0
    if not getattr(args, "force", False):
        # Protect starred by default
        cur.execute("SELECT starred FROM items WHERE id = ?", (args.id,))
        row = cur.fetchone()
        if not row:
            print(f"No item {args.id}")
            return 1
        if row[0]:
            print("Refusing to delete a starred item (use --force)", file=sys.stderr)
            return 1
    cur.execute("UPDATE items SET deleted = 1 WHERE id = ?", (args.id,))
    conn.commit()
    print(f"Deleted item {args.id} (soft)")
    return 0


def cmd_delete_source(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    # Resolve URL (supports --url or --id)
    url = getattr(args, "url", None)
    if not url and getattr(args, "id", None) is not None:
        try:
            cur.execute("SELECT url FROM feeds WHERE rowid = ?", (int(args.id),))
            row = cur.fetchone()
            url = row[0] if row else None
        except Exception:
            url = None
    if not url:
        print("Provide --url or a valid --id", file=sys.stderr)
        return 1
    if getattr(args, "undo", False):
        cur.execute("UPDATE items SET deleted = 0 WHERE feed_url = ?", (url,))
        conn.commit()
        print(f"Undeleted items for source {url}")
        return 0
    if getattr(args, "force", False):
        cur.execute("UPDATE items SET deleted = 1 WHERE feed_url = ?", (url,))
    else:
        cur.execute("UPDATE items SET deleted = 1 WHERE feed_url = ? AND COALESCE(starred,0) = 0", (url,))
    conn.commit()
    print(f"Deleted items for source {url} (soft)")
    return 0


# ---------------- Source management ---------------- #

def cmd_source_rm(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    # Resolve URL from --url or --id
    url = getattr(args, "url", None)
    if not url and getattr(args, "id", None) is not None:
        try:
            cur.execute("SELECT url FROM feeds WHERE rowid = ?", (int(args.id),))
            row = cur.fetchone()
            url = row[0] if row else None
        except Exception:
            url = None
    if not url:
        print("Provide --url or a valid --id", file=sys.stderr)
        return 1
    # Show a dry-run summary unless --yes
    cur.execute("SELECT COALESCE(title, url), archived FROM feeds WHERE url = ?", (url,))
    r = cur.fetchone()
    if not r:
        print("Source not found in DB (already removed?)")
        return 0
    name, archived = r
    cur.execute("SELECT COUNT(*) FROM items WHERE feed_url = ?", (url,))
    icount = (cur.fetchone() or (0,))[0]
    if not getattr(args, "yes", False):
        print(f"About to remove source: {name} — {url}")
        print(f"This will delete the feed and {icount} item(s) (ON DELETE CASCADE). Use --yes to confirm.")
        return 0
    # Delete feed row; cascades to items and feed_groups
    cur.execute("DELETE FROM feeds WHERE url = ?", (url,))
    conn.commit()
    if getattr(args, "vacuum", False):
        try:
            cur.execute("VACUUM")
        except Exception:
            pass
    print(f"Removed source and cascaded items: {name} — {url}")
    return 0


def cmd_archive_date(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    where = []
    params: list = []
    # Date range
    ts_field = "items.published_ts" if getattr(args, "date_field", "published") == "published" else "items.created_ts"
    if getattr(args, "on", None):
        day_start = _parse_date_arg(args.on)
        if day_start is None:
            print("Invalid --on date", file=sys.stderr)
            return 1
        day_end = day_start + 24*3600
        where.append(f"{ts_field} >= ? AND {ts_field} < ?")
        params.extend([day_start, day_end])
    else:
        since_ts = _parse_date_arg(getattr(args, "since", None))
        until_ts = _parse_date_arg(getattr(args, "until", None))
        if since_ts is not None:
            where.append(f"{ts_field} >= ?")
            params.append(since_ts)
        if until_ts is not None:
            where.append(f"{ts_field} < ?")
            params.append(until_ts)
        if since_ts is None and until_ts is None:
            print("Provide --since/--until or --on", file=sys.stderr)
            return 1
    # Group filter (OR)
    glist = _parse_groups_arg(getattr(args, "group", None))
    if glist:
        placeholders = ",".join(["?"] * len(glist))
        where.append(f"items.feed_url IN (SELECT url FROM feed_groups WHERE grp IN ({placeholders}))")
        params.extend(glist)
    # Source filter (url or id)
    src = getattr(args, "source", None)
    if src:
        url_val = None
        try:
            rid = int(src)
            cur.execute("SELECT url FROM feeds WHERE rowid = ?", (rid,))
            row = cur.fetchone()
            if row:
                url_val = row[0]
        except Exception:
            url_val = None
        if not url_val:
            url_val = src
        where.append("items.feed_url = ?")
        params.append(url_val)
    # Build final WHERE and include starred protection on archive
    where_sql = " AND ".join(where) if where else "1"
    if args.undo:
        sql = f"UPDATE items SET deleted = 0 WHERE {where_sql}"
        cur.execute(sql, params)
    else:
        sql = f"UPDATE items SET deleted = 1 WHERE {where_sql} AND items.starred = 0"
        cur.execute(sql, params)
    affected = cur.rowcount
    conn.commit()
    print(("Unarchived" if args.undo else "Archived") + f" {affected} item(s)")
    return 0


def cmd_star_add(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    cur.executemany("UPDATE items SET starred = 1 WHERE id = ?", [(i,) for i in args.ids])
    conn.commit()
    print(f"Starred {len(args.ids)} item(s)")
    return 0


def cmd_star_remove(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    cur.executemany("UPDATE items SET starred = 0 WHERE id = ?", [(i,) for i in args.ids])
    conn.commit()
    print(f"Unstarred {len(args.ids)} item(s)")
    return 0


def cmd_star(args):
    if getattr(args, "undo", False):
        return cmd_star_remove(args)
    else:
        return cmd_star_add(args)

def cmd_copy(args):
    conn = db_conn()
    init_db(conn)
    cfg = read_config()
    # Determine effective part: CLI overrides config default; map url->link
    part_raw = args.part or cfg.get("copy_default_part", "url")
    part = normalize_part(part_raw)
    pieces: list[str] = []
    missing: list[int] = []
    for iid in args.ids:
        item = get_item(conn, iid)
        if not item:
            missing.append(iid)
            continue
        data = item.get(part) if part in ("title", "summary", "content", "link") else None
        if data is None:
            data = ""
        # Determine plain: CLI flag OR config default
        plain_cfg = cfg_flag(cfg, "copy_default_plain", False)
        if (args.plain or plain_cfg) and part in ("content", "summary"):
            data = html_to_text(data)
        pieces.append(str(data))
    if not pieces:
        if missing:
            print(f"No items found for ids: {', '.join(map(str, missing))}", file=sys.stderr)
        return 1
    # Separator: from config or default newline; support \n and \t escapes
    sep_raw = cfg.get("copy_separator", "\n")
    sep = sep_raw.replace("\\n", "\n").replace("\\t", "\t")
    payload = sep.join(pieces)
    rc = export_to_clipboard(payload, cfg)
    if rc == 0:
        print(f"Copied {len(pieces)} item(s) to clipboard" + (f"; missing: {', '.join(map(str, missing))}" if missing else ""))
    return rc


# ---------------- Piping utility ---------------- #

def pipe_to_command(text: str, cmd: str):
    if not cmd:
        print("No pipe command provided", file=sys.stderr)
        return 1
    proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE)
    proc.communicate(input=text.encode("utf-8", errors="replace"))
    return proc.returncode or 0


# ---------------- Files export (tree) ---------------- #

def slugify(text: str, max_len: int = 80) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\-\s_]+", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        text = "item"
    return text[:max_len]


def iter_items(conn: sqlite3.Connection, group: str | None, unread_only: bool, limit: int | None):
    cur = conn.cursor()
    where = []
    params: list = []
    if group:
        glist = _parse_groups_arg(group)
        if glist:
            placeholders = ",".join(["?"] * len(glist))
            where.append(f"EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp IN ({placeholders}))")
            params.extend(glist)
    where.append("items.deleted = 0")
    if unread_only:
        where.append("items.read = 0")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
        SELECT items.id, items.title, items.summary, items.content, items.link,
               items.published_ts, feeds.grp
        FROM items JOIN feeds ON items.feed_url = feeds.url
        {where_sql}
        ORDER BY items.published_ts DESC, items.id DESC
        {limit_sql}
    """
    for row in cur.execute(sql, params):
        yield {
            "id": row[0],
            "title": row[1],
            "summary": row[2],
            "content": row[3],
            "link": row[4],
            "published_ts": row[5],
            "group": row[6],
        }


def ensure_clean_dir(path: str, clean: bool):
    if clean and os.path.isdir(path):
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(root, name))
                except FileNotFoundError:
                    pass
            for name in dirs:
                try:
                    os.rmdir(os.path.join(root, name))
                except OSError:
                    pass
    os.makedirs(path, exist_ok=True)


def write_item_file(dest_dir: str, item: dict, fmt: str, tags: list[str] | None = None):
    dt = datetime.fromtimestamp(item.get("published_ts") or 0).strftime("%Y-%m-%d %H:%M") if item.get("published_ts") else ""
    title = item.get("title") or ""
    link = item.get("link") or ""
    summary = item.get("summary") or ""
    content = item.get("content") or ""

    tags = tags or []
    if fmt == "json":
        data = {
            "id": item["id"],
            "group": item.get("group"),
            "title": title,
            "link": link,
            "published": dt,
            "summary": summary,
            "content": content,
            "tags": tags,
            "text": html_to_text(content or summary),
        }
        text = json.dumps(data, ensure_ascii=False, indent=2)
        ext = ".json"
    elif fmt == "html":
        # Basic HTML export: include lightweight header and raw body HTML
        header_html = []
        if title:
            header_html.append(f"<h1>{title}</h1>")
        meta_line = " ".join(filter(None, [f"[{item.get('group')}]" if item.get('group') else None, dt]))
        if meta_line:
            header_html.append(f"<p><em>{meta_line}</em></p>")
        if link:
            safe_link = link
            header_html.append(f"<p><a href=\"{safe_link}\">{safe_link}</a></p>")
        if tags:
            header_html.append("<p><strong>Tags:</strong> " + ", ".join(tags) + "</p>")
        body_html = content or summary or ""
        text = "\n".join(header_html) + "\n" + body_html + "\n"
        ext = ".html"
    else:
        body_text = html_to_text(content or summary) if fmt in ("md", "txt") else (content or summary)
        header = [
            f"# {title}" if fmt == "md" else title,
            f"[{item.get('group') or ''}] {dt}",
            link,
            ("Tags: " + ", ".join(tags)) if tags else "",
            "",
        ]
        text = "\n".join(header) + body_text + "\n"
        ext = ".md" if fmt == "md" else ".txt"

    fname = f"{item['id']:06d}-{slugify(title)}{ext}"
    with open(os.path.join(dest_dir, fname), "w", encoding="utf-8") as f:
        f.write(text)


def build_item_blob(item: dict, fmt: str, tags: list[str] | None = None) -> tuple[str, bytes]:
    """Build a relative file path and bytes content for an item export.
    Returns (relpath, data_bytes). relpath uses group directory and slug filename.
    """
    dt = datetime.fromtimestamp(item.get("published_ts") or 0).strftime("%Y-%m-%d %H:%M") if item.get("published_ts") else ""
    title = item.get("title") or ""
    link = item.get("link") or ""
    summary = item.get("summary") or ""
    content = item.get("content") or ""
    grp = item.get("group") or "ungrouped"
    tags = tags or []
    if fmt == "json":
        data = {
            "id": item["id"],
            "group": grp,
            "title": title,
            "link": link,
            "published": dt,
            "summary": summary,
            "content": content,
            "tags": tags,
            "text": html_to_text(content or summary),
        }
        text = json.dumps(data, ensure_ascii=False, indent=2)
        ext = ".json"
    elif fmt == "html":
        header_html = []
        if title:
            header_html.append(f"<h1>{title}</h1>")
        meta_line = " ".join(filter(None, [f"[{grp}]" if grp else None, dt]))
        if meta_line:
            header_html.append(f"<p><em>{meta_line}</em></p>")
        if link:
            header_html.append(f"<p><a href=\"{link}\">{link}</a></p>")
        if tags:
            header_html.append("<p><strong>Tags:</strong> " + ", ".join(tags) + "</p>")
        body_html = content or summary or ""
        text = "\n".join(header_html) + "\n" + body_html + "\n"
        ext = ".html"
    else:
        body_text = html_to_text(content or summary) if fmt in ("md", "txt") else (content or summary)
        header = [
            f"# {title}" if fmt == "md" else title,
            f"[{grp}] {dt}",
            link,
            ("Tags: " + ", ".join(tags)) if tags else "",
            "",
        ]
        text = "\n".join(header) + body_text + "\n"
        ext = ".md" if fmt == "md" else ".txt"
    fname = f"{item['id']:06d}-{slugify(title)}{ext}"
    relpath = os.path.join(grp, fname)
    return relpath, text.encode("utf-8")


def export_rows(conn: sqlite3.Connection, rows, dest: str, fmt: str) -> int:
    os.makedirs(dest, exist_ok=True)
    groups_done: set[str] = set()
    count = 0
    for row in rows:
        # rows may be tuples from queries (id, ts, read, grp, title, ...)
        try:
            iid = row[0]
            grp = row[3]
        except Exception:
            continue
        item = get_item(conn, iid)
        if not item:
            continue
        g = grp or item.get("group") or "ungrouped"
        gdir = os.path.join(dest, g)
        if g not in groups_done:
            os.makedirs(gdir, exist_ok=True)
            groups_done.add(g)
        tags = get_item_tag_names(conn, iid)
        write_item_file(gdir, item, fmt, tags)
        count += 1
    return count


def default_fs_dest() -> str:
    return os.path.join(rssel_home(), "fs")


def expected_item_path(item: dict, fmt: str = "md", dest: str | None = None) -> str:
    dest_dir = os.path.abspath(dest or default_fs_dest())
    grp = item.get("group") or "ungrouped"
    title = item.get("title") or ""
    fname = f"{item['id']:06d}-{slugify(title)}.{'md' if fmt == 'md' else fmt}"
    return os.path.join(dest_dir, grp, fname)


def cmd_files_sync(args):
    conn = db_conn()
    init_db(conn)
    dest = os.path.abspath(args.dest)
    ensure_clean_dir(dest, args.clean)

    # group -> dir
    groups_done: set[str] = set()
    count = 0
    for item in iter_items(conn, args.group, args.unread_only, args.limit):
        grp = item.get("group") or "ungrouped"
        gdir = os.path.join(dest, grp)
        if grp not in groups_done:
            os.makedirs(gdir, exist_ok=True)
            groups_done.add(grp)
        tags = get_item_tag_names(conn, item["id"]) if args.format in ("md", "txt", "json", "html") else []
        write_item_file(gdir, item, args.format, tags)
        count += 1
    print(f"Exported {count} items to {dest}")
    return 0


def cmd_sync(args):
    """Fetch feeds (optionally by group) and export grouped files in one step.
    Defaults to cleaned-up Markdown files under ./.rssel/fs.
    """
    # Fetch
    entries = read_sources_entries()
    if not entries:
        print("No sources configured. Run 'rssel init'.", file=sys.stderr)
        return 1
    conn = db_conn()
    init_db(conn)
    upsert_feeds(conn, entries, args.group)
    cur = conn.cursor()
    if args.group:
        glist = _parse_groups_arg(args.group)
        placeholders = ",".join(["?"] * len(glist)) if glist else "?"
        sql = f"SELECT DISTINCT f.url FROM feeds f JOIN feed_groups g ON g.url = f.url WHERE g.grp IN ({placeholders}) AND f.archived = 0"
        cur.execute(sql, glist or [args.group])
    else:
        cur.execute("SELECT url FROM feeds WHERE archived = 0")
    feed_urls = [r[0] for r in cur.fetchall()]
    total_new = 0
    for url in feed_urls:
        raw = http_get(url)
        if raw is None:
            continue
        items = parse_feed(url, raw)
        now_ts = int(datetime.now().timestamp())
        for it in items:
            cur.execute(
                """
                INSERT OR IGNORE INTO items(feed_url, guid, title, link, summary, content, published_ts, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    it["feed_url"],
                    it.get("guid"),
                    it.get("title"),
                    it.get("link"),
                    it.get("summary"),
                    it.get("content"),
                    int(it.get("published_ts") or now_ts),
                    now_ts,
                ),
            )
        conn.commit()
        new_count = conn.total_changes
        total_new += new_count
        print(f"Fetched {url}: {len(items)} items, new {new_count}")
    print(f"Fetch complete. New items: {total_new}")

    # Auto-tagging (default configurable; --no-auto-tags disables)
    cfg = read_config()
    cfg_auto = cfg_flag(cfg, "sync_auto_tags", True)
    cfg_include = cfg_flag(cfg, "sync_include_domain", False)
    try:
        cfg_max = int(cfg.get("sync_max_tags", "5"))
    except Exception:
        cfg_max = 5
    effective_auto = (getattr(args, "auto_tags", True) is not False) and cfg_auto
    # Detect CLI presence for overrides by flag text
    argv = sys.argv[1:]
    cli_has_max = any(tok == "--max-tags" for tok in argv)
    cli_has_include = any(tok == "--include-domain" for tok in argv)
    max_tags = int(getattr(args, "max_tags", cfg_max)) if cli_has_max else cfg_max
    include_domain = bool(getattr(args, "include_domain", cfg_include)) if cli_has_include else cfg_include

    if effective_auto:
        stop = load_stopwords()
        cur = conn.cursor()
        where = ["items.deleted = 0"]
        params: list = []
        if args.group:
            glist = _parse_groups_arg(args.group)
            if glist:
                placeholders = ",".join(["?"] * len(glist))
                where.append(f"EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp IN ({placeholders}))")
                params.extend(glist)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT items.id, items.title, items.summary, items.content, items.link
            FROM items JOIN feeds ON items.feed_url = feeds.url
            {where_sql}
            ORDER BY items.published_ts DESC, items.id DESC
        """
        cur.execute(sql, params)
        rows = cur.fetchall()
        processed = 0
        for (iid, title, summary, content, link) in rows:
            body_text = html_to_text(content or summary)
            tags = extract_auto_tags(title, body_text, max_tags=int(max_tags), stopwords=stop)
            if include_domain and link:
                try:
                    host = urlparse(link).hostname or ""
                    host = host.lower()
                    if host.startswith("www."):
                        host = host[4:]
                    if host:
                        tags.append(host)
                except Exception:
                    pass
            seen = set()
            tags = [t for t in tags if not (t in seen or seen.add(t))]
            replace_item_tags(conn, iid, tags)
            processed += 1
        print(f"Auto-tagged {processed} items (max={max_tags}, include_domain={'yes' if include_domain else 'no'})")

    # Export
    dest = os.path.abspath(args.dest)
    ensure_clean_dir(dest, args.clean)
    groups_done: set[str] = set()
    count = 0
    for item in iter_items(conn, args.group, args.unread_only, args.limit):
        grp = item.get("group") or "ungrouped"
        gdir = os.path.join(dest, grp)
        if grp not in groups_done:
            os.makedirs(gdir, exist_ok=True)
            groups_done.add(grp)
        tags = get_item_tag_names(conn, item["id"]) if args.format in ("md", "txt", "json", "html") else []
        write_item_file(gdir, item, args.format, tags)
        count += 1
    print(f"Exported {count} items to {dest} (format: {args.format})")
    return 0


def cmd_cold(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    where = ["items.deleted = 0"]
    params: list = []
    # group filter
    glist = _parse_groups_arg(getattr(args, "group", None))
    if glist:
        placeholders = ",".join(["?"] * len(glist))
        where.append(f"items.feed_url IN (SELECT url FROM feed_groups WHERE grp IN ({placeholders}))")
        params.extend(glist)
    # source filter (id or url)
    src = getattr(args, "source", None)
    if src:
        url_val = None
        try:
            rid = int(src)
            cur.execute("SELECT url FROM feeds WHERE rowid = ?", (rid,))
            row = cur.fetchone()
            if row:
                url_val = row[0]
        except Exception:
            url_val = None
        url_val = url_val or src
        where.append("items.feed_url = ?")
        params.append(url_val)
    # tag intersection filter
    if getattr(args, "tags", None):
        tag_list = [t.strip().lower() for t in re.split(r"[,\s]+", args.tags) if t.strip()]
        if tag_list:
            placeholders = ",".join(["?"] * len(tag_list))
            where.append(
                f"items.id IN ("
                f"  SELECT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id"
                f"  WHERE t.name IN ({placeholders})"
                f"  GROUP BY it.item_id HAVING COUNT(DISTINCT t.name) = {len(tag_list)}"
                f")"
            )
            params.extend(tag_list)
    # read/star filters
    if getattr(args, "read_only", False) and not args.unread_only:
        where.append("items.read = 1")
    if getattr(args, "unread_only", False):
        where.append("items.read = 0")
    if getattr(args, "star_only", False):
        where.append("items.starred = 1")
    # new window
    if getattr(args, "new", False):
        cfg = read_config()
        try:
            hours = int(cfg.get("new_hours", "24"))
        except Exception:
            hours = 24
        now_ts = int(datetime.now().timestamp())
        cutoff = now_ts - hours * 3600
        where.append("items.created_ts >= ?")
        params.append(cutoff)
    # date filters
    ts_field = "items.published_ts" if getattr(args, "date_field", "published") == "published" else "items.created_ts"
    if getattr(args, "on", None):
        day_start = _parse_date_arg(args.on)
        if day_start is not None:
            day_end = day_start + 24*3600
            where.append(f"{ts_field} >= ? AND {ts_field} < ?")
            params.extend([day_start, day_end])
    else:
        if getattr(args, "since", None):
            since_ts = _parse_date_arg(args.since)
            if since_ts is not None:
                where.append(f"{ts_field} >= ?")
                params.append(since_ts)
        if getattr(args, "until", None):
            until_ts = _parse_date_arg(args.until)
            if until_ts is not None:
                where.append(f"{ts_field} < ?")
                params.append(until_ts)
    if getattr(args, "query", None):
        where.append("(items.title LIKE ? OR items.summary LIKE ?)")
        q = f"%{args.query}%"
        params.extend([q, q])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    # default ordering: newest first
    limit_sql = f"LIMIT {int(args.limit)}" if getattr(args, "limit", None) else ""
    sql = f"""
        SELECT items.id, items.published_ts, items.read, feeds.grp, items.title, items.feed_url
        FROM items JOIN feeds ON items.feed_url = feeds.url
        {where_sql}
        ORDER BY items.published_ts DESC, items.id DESC
        {limit_sql}
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    # Optional highlight-only filter
    hl_terms: list[str] = []
    do_only_hl = getattr(args, "highlight_only", False)
    if do_only_hl or getattr(args, "highlight", False):
        hl_terms = load_highlight_words()
    selected_rows = []
    if do_only_hl and hl_terms:
        for (iid, ts, read, grp, title, feed_url) in rows:
            item = get_item(conn, iid)
            title_hl = (item.get("title") if item else title) or ""
            body_hl = html_to_text((item.get("content") if item else None) or (item.get("summary") if item else None) or "")
            low = (title_hl + "\n" + body_hl).lower()
            is_hl = False
            for term in hl_terms:
                t = term.strip()
                if not t:
                    continue
                if re.fullmatch(r"[\w\-]+", t, flags=re.UNICODE):
                    if re.search(rf"\b{re.escape(t)}\b", low, flags=re.IGNORECASE):
                        is_hl = True
                        break
                else:
                    if t.lower() in low:
                        is_hl = True
                        break
            if is_hl:
                selected_rows.append((iid, ts, read, grp, title, feed_url))
    else:
        selected_rows = rows
    # Prepare tar
    out_path = args.output
    gzip = not getattr(args, "no_gzip", False)
    if not out_path:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        out_path = os.path.join(rssel_home(), f"cold-{ts}.tar.gz")
    # If gzip is desired but output doesn't end with .gz/.tgz, append .gz
    if gzip and not out_path.endswith((".gz", ".tgz")):
        out_path = out_path + ".gz"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    mode = "w:gz" if gzip else "w"
    count = 0
    manifest_entries: list[dict] = []
    with tarfile.open(out_path, mode) as tf:
        for (iid, ts, read, grp, title, feed_url) in selected_rows:
            item = get_item(conn, iid)
            if not item:
                continue
            tags = get_item_tag_names(conn, iid)
            relpath, data = build_item_blob(item, args.format, tags)
            info = tarfile.TarInfo(name=relpath)
            info.size = len(data)
            info.mtime = int(item.get("published_ts") or datetime.now().timestamp())
            tf.addfile(info, io.BytesIO(data))
            count += 1
            # Manifest entry
            manifest_entries.append({
                "id": int(item.get("id")),
                "group": item.get("group"),
                "title": item.get("title") or "",
                "link": item.get("link") or "",
                "published_ts": int(item.get("published_ts") or 0),
                "date": datetime.fromtimestamp(item.get("published_ts") or 0).strftime("%Y-%m-%d %H:%M") if item.get("published_ts") else None,
                "tags": tags,
                "path": relpath,
            })
        # Add MANIFEST.json
        manifest = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": count,
            "format": args.format,
            "entries": manifest_entries,
        }
        mdata = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        minfo = tarfile.TarInfo(name="MANIFEST.json")
        minfo.size = len(mdata)
        minfo.mtime = int(datetime.now().timestamp())
        tf.addfile(minfo, io.BytesIO(mdata))
    print(f"Cold stored {count} item(s) to {os.path.abspath(out_path)} (format: {args.format})")
    return 0


# ---------------- Tagging commands ---------------- #

def cmd_tags_auto(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    stop = load_stopwords()
    where = ["items.deleted = 0"]
    params: list = []
    if args.group:
        where.append("EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp = ?)")
        params.append(args.group)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f"LIMIT {int(args.limit)}" if args.limit else ""
    sql = f"""
        SELECT items.id, items.title, items.summary, items.content, items.link
        FROM items JOIN feeds ON items.feed_url = feeds.url
        {where_sql}
        ORDER BY items.published_ts DESC, items.id DESC
        {limit_sql}
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    processed = 0
    for (iid, title, summary, content, link) in rows:
        body_text = html_to_text(content or summary)
        tags = extract_auto_tags(title, body_text, max_tags=args.max_tags, stopwords=stop)
        if args.include_domain and link:
            try:
                host = urlparse(link).hostname or ""
                host = host.lower()
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    tags.append(host)
            except Exception:
                pass
        # de-dup while preserving order
        seen = set()
        tags = [t for t in tags if not (t in seen or seen.add(t))]
        if args.dry_run:
            print(f"{iid:6d}  " + ", ".join(tags))
        else:
            replace_item_tags(conn, iid, tags)
        processed += 1
    if not args.dry_run:
        print(f"Auto-tagged {processed} items")
    return 0


def cmd_tags_list(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    where = ["items.deleted = 0"]
    params: list = []
    if args.group:
        where.append("EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp = ?)")
        params.append(args.group)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    # Sorting + limit
    tsort = getattr(args, "sort", None) or "count-desc"
    if tsort == "name":
        order_sql = "ORDER BY t.name COLLATE NOCASE"
    elif tsort == "count-asc":
        order_sql = "ORDER BY cnt ASC, t.name COLLATE NOCASE"
    else:
        order_sql = "ORDER BY cnt DESC, t.name COLLATE NOCASE"
    limit_sql = f"LIMIT {int(args.top)}" if getattr(args, "top", None) else ""
    sql = f"""
        SELECT t.name, COUNT(*) as cnt
        FROM tags t
        JOIN item_tags it ON t.id = it.tag_id
        JOIN items ON items.id = it.item_id
        JOIN feeds ON feeds.url = items.feed_url
        {where_sql}
        GROUP BY t.name
        {order_sql}
        {limit_sql}
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    for name, cnt in rows:
        print(f"{cnt:5d}  {name}")
    return 0


    


def cmd_tags_items(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    tag = (args.tag or "").strip().lower()
    if not tag:
        print("Provide --tag", file=sys.stderr)
        return 1
    # Support multiple tags (comma/space separated): require ALL to match
    tag_list = [t.strip() for t in re.split(r"[,\s]+", tag) if t.strip()]
    where = ["items.deleted = 0"]
    params: list = []
    if len(tag_list) == 1:
        where.append("t.name = ?")
        params.append(tag_list[0])
        tag_header = tag_list
    else:
        placeholders = ",".join(["?"] * len(tag_list))
        where.append(
            f"items.id IN ("
            f"  SELECT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id"
            f"  WHERE t.name IN ({placeholders})"
            f"  GROUP BY it.item_id HAVING COUNT(DISTINCT t.name) = {len(tag_list)}"
            f")"
        )
        params.extend(tag_list)
        tag_header = tag_list
    if args.group:
        where.append("EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp = ?)")
        params.append(args.group)
    if args.unread_only:
        where.append("items.read = 0")
    where_sql = " AND ".join(where)
    limit_sql = f"LIMIT {int(args.limit)}" if args.limit else ""
    sql = f"""
        SELECT items.id, items.published_ts, items.read, feeds.grp, items.title
        FROM items
        JOIN item_tags it ON it.item_id = items.id
        JOIN tags t ON t.id = it.tag_id
        JOIN feeds ON feeds.url = items.feed_url
        WHERE {where_sql}
        ORDER BY items.published_ts DESC, items.id DESC
        {limit_sql}
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    header_name = ", ".join(tag_header)
    print(f"Tags: {header_name}  (items: {len(rows)})")
    opts = resolve_display_opts(args)
    for (iid, ts, read, grp, title) in rows:
        mark = " " if read else "*"
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
        if opts["show_url"] or opts["show_tags"] or opts["show_path"] or opts["show_snippet"] or opts["show_date"]:
            id_s = _maybe(str(iid), opts["color"], 2)
            mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
            grp_s = f"[{_maybe(grp, opts["color"], 36)}]"
            title_s = _maybe(title or '', opts["color"], 1)
            print(f"{id_s} {mark_s} {grp_s} {title_s}")
        else:
            id_s = _maybe(f"{iid:6d}", opts["color"], 2)
            mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
            grp_s = f"[{_maybe(grp, opts["color"], 36)}]"
            dt_s = _maybe(dt, opts["color"], 2)
            title_s = _maybe(title or '', opts["color"], 1)
            print(f"{id_s} {mark_s} {grp_s} {dt_s}  {title_s}")
        if opts.get("show_url") or opts.get("show_tags") or opts.get("show_path") or opts.get("show_snippet") or opts.get("show_date") or getattr(args, "show_source", False):
            item = get_item(conn, iid)
            if getattr(args, "show_source", False):
                o2 = dict(opts)
                o2["show_source"] = True
                _print_meta_block(conn, item, dt, o2)
            else:
                _print_meta_block(conn, item, dt, opts)
    return 0


def cmd_tags_map(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    # Get tags ordered by count (optionally filtered by group)
    where = ["items.deleted = 0"]
    params: list = []
    if args.group:
        where.append("EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp = ?)")
        params.append(args.group)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    top_limit = f"LIMIT {int(args.top)}" if args.top else ""
    having_sql = ""
    params_tags = list(params)
    if getattr(args, "min_count", None):
        having_sql = "HAVING COUNT(*) >= ?"
        params_tags.append(int(args.min_count))
    # Sort order for tags
    tsort = getattr(args, "sort", None) or "count-desc"
    if tsort == "name":
        order_tags_sql = "ORDER BY t.name COLLATE NOCASE"
    elif tsort == "count-asc":
        order_tags_sql = "ORDER BY cnt ASC, t.name COLLATE NOCASE"
    else:
        order_tags_sql = "ORDER BY cnt DESC, t.name COLLATE NOCASE"
    sql_tags = f"""
        SELECT t.id, t.name, COUNT(*) as cnt
        FROM tags t
        JOIN item_tags it ON t.id = it.tag_id
        JOIN items ON items.id = it.item_id
        JOIN feeds ON feeds.url = items.feed_url
        {where_sql}
        GROUP BY t.id, t.name
        {having_sql}
        {order_tags_sql}
        {top_limit}
    """
    cur.execute(sql_tags, params_tags)
    tags = cur.fetchall()
    for (tid, name, cnt) in tags:
        # Fetch items for this tag
        sql_items = """
            SELECT items.id, items.published_ts, items.read, feeds.grp, items.title
            FROM items
            JOIN item_tags it ON it.item_id = items.id
            JOIN feeds ON feeds.url = items.feed_url
            WHERE it.tag_id = ? AND items.deleted = 0
            ORDER BY items.published_ts DESC, items.id DESC
            LIMIT ?
        """
        cur.execute(sql_items, (tid, int(args.max_per_tag)))
        rows = cur.fetchall()
        # Default to compact unless --detailed is explicitly set
        if getattr(args, "compact", False) or not getattr(args, "detailed", False):
            ids = [r[0] for r in rows]
            # Python-list style on one line
            tname = _maybe(name, True, 36) if getattr(args, "color", False) else name
            print(f"{tname} ({cnt}): [" + ", ".join(str(i) for i in ids) + "]")
        else:
            hname = _maybe(name, True, 36) if getattr(args, "color", False) else name
            print(f"[{hname}] ({cnt})")
            for (iid, ts, read, grp, title) in rows:
                mark = " " if read else "*"
                dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
                if getattr(args, "color", False):
                    id_s = _maybe(f"{iid:6d}", True, 2)
                    grp_s = f"[{_maybe(grp, True, 36)}]"
                    dt_s = _maybe(dt, True, 2)
                    title_s = _maybe(title or '', True, 1)
                    print(f"  {id_s} {mark} {grp_s} {dt_s}  {title_s}")
                else:
                    print(f"  {iid:6d} {mark} [{grp}] {dt}  {title or ''}")
    return 0


def _build_source_filter(cur: sqlite3.Cursor, src: str | None, src_id: int | None = None) -> tuple[str, list]:
    """Build a SQL filter and params for restricting by source.
    Accepts either a URL/string (which may be a numeric string id) or an explicit numeric id.
    Returns (sql, params) like ("items.feed_url = ?", [url]).
    """
    # Explicit id takes precedence
    if src_id is not None:
        try:
            cur.execute("SELECT url FROM feeds WHERE rowid = ?", (int(src_id),))
            row = cur.fetchone()
            if row and row[0]:
                return "items.feed_url = ?", [row[0]]
        except Exception:
            return "", []
        return "", []
    if not src:
        return "", []
    try:
        rid = int(src)
        cur.execute("SELECT url FROM feeds WHERE rowid = ?", (rid,))
        row = cur.fetchone()
        if row:
            return "items.feed_url = ?", [row[0]]
    except Exception:
        pass
    return "items.feed_url = ?", [src]


def cmd_purge(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()

    removed_deleted = 0
    removed_older = 0
    removed_tags = 0

    def _apply_group(where: list[str], params: list):
        glist = _parse_groups_arg(getattr(args, "group", None))
        if glist:
            placeholders = ",".join(["?"] * len(glist))
            where.append(f"items.feed_url IN (SELECT url FROM feed_groups WHERE grp IN ({placeholders}))")
            params.extend(glist)

    # Purge items marked as deleted
    if getattr(args, "deleted", False):
        where = ["items.deleted = 1"]
        params: list = []
        _apply_group(where, params)
        src_sql, src_params = _build_source_filter(cur, getattr(args, "source", None), getattr(args, "source_id", None))
        if src_sql:
            where.append(src_sql)
            params.extend(src_params)
        where_sql = " AND ".join(where)
        cur.execute(f"SELECT COUNT(*) FROM items WHERE {where_sql}", params)
        cnt = (cur.fetchone() or (0,))[0]
        if not getattr(args, "dry_run", False):
            cur.execute(f"DELETE FROM items WHERE {where_sql}", params)
            conn.commit()
        removed_deleted = cnt

    # Purge read items older than cutoff (non-starred only)
    cutoff_ts = None
    if getattr(args, "older_days", None):
        try:
            cutoff_ts = int(datetime.now().timestamp()) - int(args.older_days) * 86400
        except Exception:
            cutoff_ts = None
    if getattr(args, "before", None):
        ts = _parse_date_arg(args.before)
        if ts is not None:
            cutoff_ts = ts if cutoff_ts is None else min(cutoff_ts, ts)
    if cutoff_ts is not None:
        where = ["items.read = 1", "COALESCE(items.starred, 0) = 0", "COALESCE(items.deleted, 0) = 0", "items.published_ts < ?"]
        params: list = [cutoff_ts]
        _apply_group(where, params)
        src_sql, src_params = _build_source_filter(cur, getattr(args, "source", None), getattr(args, "source_id", None))
        if src_sql:
            where.append(src_sql)
            params.extend(src_params)
        where_sql = " AND ".join(where)
        cur.execute(f"SELECT COUNT(*) FROM items WHERE {where_sql}", params)
        cnt = (cur.fetchone() or (0,))[0]
        if not getattr(args, "dry_run", False):
            cur.execute(f"DELETE FROM items WHERE {where_sql}", params)
            conn.commit()
        removed_older = cnt

    # Clean up unused tags
    if getattr(args, "clean_tags", False):
        cur.execute("SELECT COUNT(*) FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM item_tags)")
        tcnt = (cur.fetchone() or (0,))[0]
        if not getattr(args, "dry_run", False):
            cur.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM item_tags)")
            conn.commit()
        removed_tags = tcnt

    # Optional VACUUM
    if getattr(args, "vacuum", False) and not getattr(args, "dry_run", False):
        try:
            cur.execute("VACUUM")
        except Exception:
            pass

    # Summary
    parts = []
    if getattr(args, "deleted", False):
        parts.append(f"purged_deleted={removed_deleted}")
    if cutoff_ts is not None:
        parts.append(f"purged_older={removed_older}")
    if getattr(args, "clean_tags", False):
        parts.append(f"removed_tags={removed_tags}")
    if getattr(args, "vacuum", False):
        parts.append("vacuumed")
    if not parts:
        print("Nothing to do. Use --deleted and/or --before/--older-days, optionally --clean-tags/--vacuum.")
    else:
        prefix = "[dry-run] " if getattr(args, "dry_run", False) else ""
        print(prefix + ", ".join(parts))
    return 0


def cmd_tags_compact(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    where = ["items.deleted = 0"]
    params: list = []
    if args.group:
        where.append("EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp = ?)")
        params.append(args.group)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    # Sorting, limit, and min-count for compact
    top_limit = f"LIMIT {int(args.top)}" if args.top else ""
    tsort = getattr(args, "sort", None) or "count-desc"
    if tsort == "name":
        order_tags_sql = "ORDER BY t.name COLLATE NOCASE"
    elif tsort == "count-asc":
        order_tags_sql = "ORDER BY cnt ASC, t.name COLLATE NOCASE"
    else:
        order_tags_sql = "ORDER BY cnt DESC, t.name COLLATE NOCASE"
    having_sql = ""
    params_tags = list(params)
    if getattr(args, "min_count", None):
        having_sql = "HAVING COUNT(*) >= ?"
        params_tags.append(int(args.min_count))
    sql_tags = f"""
        SELECT t.id, t.name, COUNT(*) as cnt
        FROM tags t
        JOIN item_tags it ON t.id = it.tag_id
        JOIN items ON items.id = it.item_id
        JOIN feeds ON feeds.url = items.feed_url
        {where_sql}
        GROUP BY t.id, t.name
        {having_sql}
        {order_tags_sql}
        {top_limit}
    """
    cur.execute(sql_tags, params_tags)
    tags = cur.fetchall()
    chunks: list[str] = []
    for (tid, name, cnt) in tags:
        sql_items = """
            SELECT items.id
            FROM items
            JOIN item_tags it ON it.item_id = items.id
            WHERE it.tag_id = ? AND items.deleted = 0
            ORDER BY items.published_ts DESC, items.id DESC
            LIMIT ?
        """
        cur.execute(sql_items, (tid, int(args.max_per_tag)))
        ids = [str(r[0]) for r in cur.fetchall()]
        if getattr(args, "color", False):
            name_s = _maybe(name, True, 36)
            cnt_s = _maybe(str(cnt), True, 2)
            chunk = f"{name_s} ({cnt_s}): [" + ", ".join(ids) + "]"
        else:
            chunk = f"{name} ({cnt}): [" + ", ".join(ids) + "]"
        chunks.append(chunk)
    print("; ".join(chunks))
    return 0


def cmd_pick_tags(args):
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    where = ["items.deleted = 0"]
    params: list = []
    if args.group:
        where.append("EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp = ?)")
        params.append(args.group)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT t.name, COUNT(*) as cnt
        FROM tags t
        JOIN item_tags it ON t.id = it.tag_id
        JOIN items ON items.id = it.item_id
        JOIN feeds ON feeds.url = items.feed_url
        {where_sql}
        GROUP BY t.name
        ORDER BY cnt DESC, t.name
    """
    cur.execute(sql, params)
    tag_rows = cur.fetchall()
    if not tag_rows:
        print("No tags to pick from. Try 'rssel tags auto'.")
        return 0
    lines = "\n".join([f"{cnt:5d}\t{name}" for (name, cnt) in tag_rows])
    selected = None
    if not args.no_fzf:
        # Build fzf command with optional preview
        fzf_cmd = detect_fzf()
        if fzf_cmd:
            fzf_cmd = fzf_cmd + ["--delimiter", "\t", "--with-nth", "2..", "--prompt", "tags>"]
            if args.preview:
                py = shutil.which('python3') or 'python3'
                # Preview: show top items for highlighted tag
                # Use field {2} (tag name) from the fzf line
                fzf_cmd += ["--preview", f"{py} {os.path.abspath(__file__)} tags items --tag {{2}} --limit 20"]
            try:
                proc = subprocess.Popen(fzf_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                out, _ = proc.communicate(input=lines.encode("utf-8", errors="replace"))
                if proc.returncode == 0 and out:
                    selected = out.decode("utf-8", errors="replace").strip()
            except FileNotFoundError:
                selected = None
    if not selected:
        selected = basic_picker(lines)
    if not selected:
        return 0
    parts = selected.split("\t", 1)
    tag_name = parts[1] if len(parts) > 1 else parts[0].strip()

    where_i = ["t.name = ?", "items.deleted = 0"]
    params_i: list = [tag_name]
    if args.group:
        glist = _parse_groups_arg(args.group)
        if glist:
            placeholders = ",".join(["?"] * len(glist))
            where_i.append(f"EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp IN ({placeholders}))")
            params_i.extend(glist)
    if args.unread_only:
        where_i.append("items.read = 0")
    where_i_sql = " AND ".join(where_i)
    limit_sql = f"LIMIT {int(args.limit)}" if args.limit else ""
    sql_i = f"""
        SELECT items.id, items.published_ts, items.read, feeds.grp, items.title, items.summary
        FROM items
        JOIN item_tags it ON it.item_id = items.id
        JOIN tags t ON t.id = it.tag_id
        JOIN feeds ON feeds.url = items.feed_url
        WHERE {where_i_sql}
        ORDER BY items.published_ts DESC, items.id DESC
        {limit_sql}
    """
    # Apply additional required tags intersection if provided
    extra_tags = []
    if getattr(args, "tags", None):
        extra_tags = [t.strip().lower() for t in re.split(r"[,\s]+", args.tags or "") if t.strip()]
    if extra_tags:
        placeholders = ",".join(["?"] * len(extra_tags))
        sql_i += f" AND items.id IN ("
        sql_i += f"  SELECT it2.item_id FROM item_tags it2 JOIN tags t2 ON t2.id = it2.tag_id"
        sql_i += f"  WHERE t2.name IN ({placeholders})"
        sql_i += f"  GROUP BY it2.item_id HAVING COUNT(DISTINCT t2.name) = {len(extra_tags)}"
        sql_i += f")"
        params_i.extend(extra_tags)
    cur.execute(sql_i, params_i)
    rows = cur.fetchall()
    print(f"Tag: {tag_name}  (items: {len(rows)})")
    use_fzf_items = (not args.no_fzf) and bool(detect_fzf())
    if use_fzf_items and rows:
        # Interactive filter; capture query and re-run SQL with LIKE
        # Build lines with a hidden search column (title + summary)
        fzf_lines = []
        for (iid, ts, read, grp, title, summary) in rows:
            mark = (" " if read else "*") + " "
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
            # Hidden search field: title + summary, single-line, tabs stripped
            txt = html_to_text(summary or "")
            txt = " ".join(txt.split()).replace("\t", " ")
            search = f"{title or ''} {txt}".strip()
            fzf_lines.append(f"{iid}\t{mark}\t[{grp}]\t{dt}\t{title or ''}\t{search}")
        lines = "\n".join(fzf_lines)
        cmd = detect_fzf() + [
            "--with-nth", "2..5",
            "--delimiter", "\t",
            "--prompt", f"{tag_name}>",
            "--print-query",
            "--multi",
            "--bind", "enter:select-all+accept",
        ]
        if args.preview:
            cmd += ["--preview", f"{shutil.which('python3') or 'python3'} {os.path.abspath(__file__)} preview --id {{1}}", "--preview-window", "right:60%:wrap"]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            out, _ = proc.communicate(input=lines.encode("utf-8", errors="replace"))
            if proc.returncode == 0 and out:
                out_lines = out.decode("utf-8", errors="replace").splitlines()
                selected_order: list[int] = []
                for ln in out_lines[1:]:
                    iid = parse_selected_id(ln)
                    if iid is not None:
                        selected_order.append(iid)
                if selected_order:
                    selset = set(selected_order)
                    rows = [r for r in rows if r[0] in selset]
                    order_index = {iid: idx for idx, iid in enumerate(selected_order)}
                    rows.sort(key=lambda r: order_index.get(r[0], 1_000_000))
        except FileNotFoundError:
            pass
    rows_to_print = rows
    # Print like list
    opts = resolve_display_opts(args)
    # Optional export
    if getattr(args, "export", False) and rows_to_print:
        cfg = read_config()
        dest = args.dest or cfg.get("export_dir") or os.path.join(rssel_home(), "fs")
        fmt = args.format or cfg.get("export_format") or "md"
        n = export_rows(conn, rows_to_print, os.path.abspath(dest), fmt)
        print(f"Exported {n} items to {os.path.abspath(dest)} (format: {fmt})")
    # Optional export
    if getattr(args, "export", False) and rows_to_print:
        cfg = read_config()
        dest = args.dest or cfg.get("export_dir") or os.path.join(rssel_home(), "fs")
        fmt = args.format or cfg.get("export_format") or "md"
        n = export_rows(conn, rows_to_print, os.path.abspath(dest), fmt)
        print(f"Exported {n} items to {os.path.abspath(dest)} (format: {fmt})")
    for (iid, ts, read, grp, title, *rest) in rows_to_print:
        mark = " " if read else "*"
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
        if opts["show_url"] or opts["show_tags"] or opts["show_path"] or opts["show_snippet"] or opts["show_date"]:
            id_s = _maybe(str(iid), opts["color"], 2)
            mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
            grp_s = f"[{_maybe(grp, opts["color"], 36)}]"
            title_s = _maybe(title or '', opts["color"], 1)
            print(f"{id_s} {mark_s} {grp_s} {title_s}")
        else:
            id_s = _maybe(f"{iid:6d}", opts["color"], 2)
            mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
            grp_s = f"[{_maybe(grp, opts["color"], 36)}]"
            dt_s = _maybe(dt, opts["color"], 2)
            title_s = _maybe(title or '', opts["color"], 1)
            print(f"{id_s} {mark_s} {grp_s} {dt_s}  {title_s}")
        if opts["show_url"] or opts["show_tags"] or opts["show_path"] or opts["show_snippet"] or opts["show_date"]:
            item = get_item(conn, iid)
            _print_meta_block(conn, item, dt, opts)
    return 0


# ---------------- Picker (fzf) ---------------- #

def detect_fzf() -> list[str] | None:
    # Allow override via RSSEL_PICKER
    env = os.environ.get("RSSEL_PICKER")
    if env:
        parts = env.split()
        if shutil.which(parts[0]):
            return parts
    for c in ("fzf", "fzf-tmux"):
        if shutil.which(c):
            return [c]
    return None


    


    


    


def basic_picker(lines: str):
    # Simple fallback: print with numbers; read selection from stdin
    opts = []
    for idx, ln in enumerate(lines.splitlines(), 1):
        print(f"{idx:3d} {ln}")
        opts.append(ln)
    try:
        sel = input("Select number (empty to cancel): ").strip()
    except EOFError:
        return None
    if not sel:
        return None
    try:
        n = int(sel)
    except ValueError:
        return None
    if 1 <= n <= len(opts):
        return opts[n - 1]
    return None


def parse_selected_id(line: str | None) -> int | None:
    if not line:
        return None
    # id is first tab-separated field
    tok = line.split("\t", 1)[0].strip()
    try:
        return int(tok)
    except ValueError:
        return None


def cmd_pick(args):
    """Fuzzy filter items, then print them like `list` with show options.
    - If fzf is available and not --no-fzf: open fzf over the current filtered list.
      - With --multi, you can select multiple items; otherwise single.
      - If you select items, only those are printed. If you cancel, prints the whole list.
    - Without fzf or with --no-fzf: prints the whole filtered list.
    """
    conn = db_conn()
    init_db(conn)
    # Build rows like list (supports --tags)
    def build_rows(q: str | None):
        cur = conn.cursor()
        where = ["items.deleted = 0"]
        params: list = []
        if args.group:
            where.append("EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp = ?)")
            params.append(args.group)
        # tags ALL-match
        if getattr(args, "tags", None):
            tag_list = [t.strip().lower() for t in re.split(r"[,\s]+", args.tags or "") if t.strip()]
            if tag_list:
                placeholders = ",".join(["?"] * len(tag_list))
                where.append(
                    f"items.id IN ("
                    f"  SELECT it.item_id FROM item_tags it JOIN tags t ON t.id = it.tag_id"
                    f"  WHERE t.name IN ({placeholders})"
                    f"  GROUP BY it.item_id HAVING COUNT(DISTINCT t.name) = {len(tag_list)}"
                    f")"
                )
                params.extend(tag_list)
        if args.unread_only:
            where.append("items.read = 0")
        elif getattr(args, "read_only", False):
            where.append("items.read = 1")
        if getattr(args, "star_only", False):
            where.append("items.starred = 1")
        if getattr(args, "new", False):
            cfg = read_config()
            try:
                hours = int(cfg.get("new_hours", "24"))
            except Exception:
                hours = 24
            now_ts = int(datetime.now().timestamp())
            cutoff = now_ts - hours * 3600
            where.append("items.created_ts >= ?")
            params.append(cutoff)
        # Date filters
        ts_field = "items.published_ts" if getattr(args, "date_field", "published") == "published" else "items.created_ts"
        if getattr(args, "on", None):
            day_start = _parse_date_arg(args.on)
            if day_start is not None:
                day_end = day_start + 24*3600
                where.append(f"{ts_field} >= ? AND {ts_field} < ?")
                params.extend([day_start, day_end])
        else:
            if getattr(args, "since", None):
                since_ts = _parse_date_arg(args.since)
                if since_ts is not None:
                    where.append(f"{ts_field} >= ?")
                    params.append(since_ts)
            if getattr(args, "until", None):
                until_ts = _parse_date_arg(args.until)
                if until_ts is not None:
                    where.append(f"{ts_field} < ?")
                    params.append(until_ts)
        if q:
            where.append("(items.title LIKE ? OR items.summary LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        # Effective limit: CLI --limit or config list_max
        try:
            eff_limit = int(args.limit) if args.limit else int(read_config().get("list_max", "2000"))
        except Exception:
            eff_limit = None
        limit_sql = f"LIMIT {eff_limit}" if eff_limit else ""
        ts_field = "items.published_ts" if getattr(args, "date_field", "published") == "published" else "items.created_ts"
        if getattr(args, "sort_id_rev", False):
            order_by = "items.id ASC"
        elif getattr(args, "sort_id", False):
            order_by = "items.id DESC"
        elif getattr(args, "sort_name", False):
            order_by = "LOWER(COALESCE(items.title,'')) ASC, items.id ASC"
        elif getattr(args, "sort_group", False):
            order_by = f"feeds.grp COLLATE NOCASE ASC, {ts_field} DESC, items.id DESC"
        elif getattr(args, "sort_date_old", False):
            order_by = f"{ts_field} ASC, items.id ASC"
        elif getattr(args, "sort_count", False):
            order_by = f"(SELECT COUNT(*) FROM item_tags it2 WHERE it2.item_id = items.id) DESC, {ts_field} DESC, items.id DESC"
        else:
            order_by = f"{ts_field} DESC, items.id DESC"
        sql = f"""
            SELECT items.id, items.published_ts, items.read, feeds.grp, items.title, items.summary
            FROM items JOIN feeds ON items.feed_url = feeds.url
            {where_sql}
            ORDER BY {order_by}
            {limit_sql}
        """
        cur.execute(sql, params)
        return cur.fetchall()

    rows = build_rows(args.query)
    if not rows:
        print("No items.")
        return 0
    use_fzf = (not args.no_fzf) and bool(detect_fzf())
    if use_fzf:
        # Let user filter interactively; capture the query and re-run DB with it
        # Build lines with hidden search column (title + summary)
        fzf_lines = []
        for (iid, ts, read, grp, title, summary) in rows:
            mark = (" " if read else "*") + " "
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
            txt = html_to_text(summary or "")
            txt = " ".join(txt.split()).replace("\t", " ")
            search = f"{title or ''} {txt}".strip()
            fzf_lines.append(f"{iid}\t{mark}\t[{grp}]\t{dt}\t{title or ''}\t{search}")
        lines = "\n".join(fzf_lines)
        cmd = detect_fzf() + [
            "--with-nth", "2..5",
            "--delimiter", "\t",
            "--prompt", "pick>",
            "--print-query",
            "--multi",
            "--bind", "enter:select-all+accept",
        ]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            out, _ = proc.communicate(input=lines.encode("utf-8", errors="replace"))
            if proc.returncode == 0 and out:
                out_lines = out.decode("utf-8", errors="replace").splitlines()
                # First line is the query; subsequent lines are all matches selected via bind
                selected_order: list[int] = []
                for ln in out_lines[1:]:
                    iid = parse_selected_id(ln)
                    if iid is not None:
                        selected_order.append(iid)
                if selected_order:
                    selset = set(selected_order)
                    rows = [r for r in rows if r[0] in selset]
                    order_index = {iid: idx for idx, iid in enumerate(selected_order)}
                    rows.sort(key=lambda r: order_index.get(r[0], 1_000_000))
        except FileNotFoundError:
            pass
    rows_to_print = rows
    # Print like list
    opts = resolve_display_opts(args)
    for (iid, ts, read, grp, title, *rest) in rows_to_print:
        mark = " " if read else "*"
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
        if opts["show_url"] or opts["show_tags"] or opts["show_path"] or opts["show_snippet"] or opts["show_date"]:
            id_s = _maybe(str(iid), opts["color"], 2)
            mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
            grp_s = f"[{_maybe(grp, opts["color"], 36)}]"
            title_s = _maybe(title or '', opts["color"], 1)
            print(f"{id_s} {mark_s} {grp_s} {title_s}")
        else:
            id_s = _maybe(f"{iid:6d}", opts["color"], 2)
            mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
            grp_s = f"[{_maybe(grp, opts["color"], 36)}]"
            dt_s = _maybe(dt, opts["color"], 2)
            title_s = _maybe(title or '', opts["color"], 1)
            print(f"{id_s} {mark_s} {grp_s} {dt_s}  {title_s}")
        if opts["show_url"] or opts["show_tags"] or opts["show_path"] or opts["show_snippet"] or opts["show_date"]:
            item = get_item(conn, iid)
            _print_meta_block(conn, item, dt, opts)
    return 0


    


def build_parser():
    p = argparse.ArgumentParser(prog=APP_NAME, description="Lightweight, self-contained CLI RSS reader")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Create local config, sources, and DB")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("sources", help="Show configured groups and feeds")
    sp.add_argument("--with-db", action="store_true", help="Show DB status: archived flag and current item counts")
    sp.add_argument("--include-db-only", action="store_true", help="When used with --with-db, also list DB-only sources not present in config")
    sp.set_defaults(func=cmd_sources)

    sp = sub.add_parser("fetch", help="Fetch feeds and cache items")
    sp.add_argument("--group", "-g", help="Only fetch this group")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("list", help="List cached items")
    sp.add_argument("--group", "-g", help="Filter by group")
    sp.add_argument("--tags", help="Comma/space separated tag names; item must have ALL")
    sp.add_argument("--limit", "-n", type=int, help="Limit number of items")
    sp.add_argument("--unread-only", action="store_true", help="Only show unread items")
    sp.add_argument("--read", dest="read_only", action="store_true", help="Only show read items")
    sp.add_argument("--star", dest="star_only", action="store_true", help="Only show starred (favorite) items")
    sp.add_argument("--new", action="store_true", help="Only items added in the last 24 hours")
    sp.add_argument("--query", "-q", help="Search in title/summary")
    sp.add_argument("--since", help="Filter by date/time >= (e.g., 2025-10-16 or 2025-10-16 12:00 or 'today')")
    sp.add_argument("--until", help="Filter by date/time < (exclusive)")
    sp.add_argument("--on", help="Filter items on a specific day (YYYY-MM-DD or 'today')")
    sp.add_argument("--date-field", choices=["published", "created"], default="published", help="Which timestamp to use for date filters (default: published)")
    sp.add_argument("--source", help="Filter by a source (url or numeric id)")
    sp.add_argument("--sources", action="store_true", help="List sources summary (ids, names, urls, counts)")
    sp.add_argument("--list-sources", action="store_true", help="Alias: list sources summary (ids, names, urls, counts)")
    sp.add_argument("--list-tags", action="store_true", help="List all tags with counts (optional: filter by --group)")
    sp.add_argument("--tags-sort", choices=["name","count-asc","count-desc"], help="Sort order for --list-tags output (default: count-desc)")
    sp.add_argument("--tags-top", type=int, help="Limit number of tags shown for --list-tags")
    # Sorting options (mutually exclusive; last one wins if multiple)
    sp.add_argument("--sort-id", action="store_true", help="Sort by item id (newest first)")
    sp.add_argument("--sort-id-rev", action="store_true", help="Sort by item id (oldest first)")
    sp.add_argument("--sort-name", action="store_true", help="Sort by title (A→Z)")
    sp.add_argument("--sort-date-new", action="store_true", help="Sort by date (newest first)")
    sp.add_argument("--sort-date-old", action="store_true", help="Sort by date (oldest first)")
    sp.add_argument("--sort-group", action="store_true", help="Sort by group name (A→Z), then date newest")
    sp.add_argument("--sort-count", action="store_true", help="Sort by tag count (desc), then date newest")
    sp.add_argument("--highlight", action="store_true", help="Mark items matching highlight word list with '!' (see highlight_words_file in config)")
    sp.add_argument("--highlight-only", action="store_true", help="Only show items that match the highlight word list")
    sp.add_argument("--grid", action="store_true", help="Align output as a grid (columns)")
    sp.add_argument("--grid-meta", help="In --grid mode, show selected metadata rows (comma/space list of: date,path,url,tags,source,snippet)")
    sp.add_argument("--json", action="store_true", help="Output items as a JSON array (includes base fields + url/path/tags/source/snippet)")
    sp.add_argument("--no-show-tags", dest="no_show_tags", action="store_true", help="Force-hide tags even if enabled in config")
    sp.add_argument("--show-url", action="store_true", help="Show URL as an indented metadata line")
    sp.add_argument("--show-tags", action="store_true", help="Show tags as an indented metadata line")
    sp.add_argument("--show-path", action="store_true", help="Show expected file path as an indented metadata line")
    sp.add_argument("--show-date", action="store_true", help="Show published date as an indented metadata line")
    sp.add_argument("--show-snippet", action="store_true", help="Show a short text snippet under each item")
    sp.add_argument("--show-source", action="store_true", help="Show the source (title or URL) as an indented metadata line")
    sp.add_argument("--color", action="store_true", help="Colorize output (titles, groups, markers)")
    sp.add_argument("--export", action="store_true", help="Export the filtered items to files")
    sp.add_argument("--nocolor", action="store_true", help="Disable ANSI colors in output (overrides --color and config)")
    sp.add_argument("--dest", help="Export destination directory (defaults to export_dir in config)")
    sp.add_argument("--format", choices=["md", "txt", "json", "html"], help="Export format (defaults to export_format in config)")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("export", help="Export one or many items")
    sp.add_argument("ids", type=int, nargs="+", help="Item ID(s)")
    sp.add_argument("--to", choices=["stdout", "editor", "clipboard", "file"], default="stdout", help="Destination: stdout/editor/clipboard or file(s)")
    sp.add_argument("--part", choices=["title", "summary", "content", "link", "url"], help="Field for stdout/editor/clipboard (default: content)")
    sp.add_argument("--plain", action="store_true", help="Convert HTML to plain text for content/summary (stdout/editor/clipboard)")
    sp.add_argument("--dest", help="Directory for --to file (defaults to external_export_dir in config or current dir)")
    sp.add_argument("--format", choices=["md", "txt", "json", "html"], help="File format for --to file (defaults to external_export_format in config)")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("mark", help="Mark an item read or unread")
    sp.add_argument("id", type=int)
    sp.add_argument("state", choices=["read", "unread"]) 
    sp.set_defaults(func=cmd_mark)

    sp = sub.add_parser("view", help="Read an article in a pager")
    sp.add_argument("id", type=int, nargs="?", help="Item ID")
    sp.add_argument("--next", action="store_true", help="Open next unread item")
    sp.add_argument("--group", "-g", help="Restrict --next to a group")
    sp.add_argument("--raw", action="store_true", help="Show raw HTML instead of plain text")
    sp.add_argument("--mark-read", action="store_true", help="(Legacy) Mark item as read after viewing (default behavior)")
    sp.add_argument("--no-mark-read", action="store_true", help="Do not mark item as read after viewing")
    sp.set_defaults(func=cmd_view)

    # 'read' is an alias of 'view'
    sp_read = sub.add_parser("read", help="Alias for 'view'")
    sp_read.add_argument("id", type=int, nargs="?", help="Item ID")
    sp_read.add_argument("--next", action="store_true", help="Open next unread item")
    sp_read.add_argument("--group", "-g", help="Restrict --next to a group")
    sp_read.add_argument("--raw", action="store_true", help="Show raw HTML instead of plain text")
    sp_read.add_argument("--mark-read", action="store_true", help="(Legacy) Mark item as read after viewing (default behavior)")
    sp_read.add_argument("--no-mark-read", action="store_true", help="Do not mark item as read after viewing")
    sp_read.set_defaults(func=cmd_view)

    # 'v' is a short alias of 'view'
    sp_v = sub.add_parser("v", help="Alias for 'view'")
    sp_v.add_argument("id", type=int, nargs="?", help="Item ID")
    sp_v.add_argument("--next", action="store_true", help="Open next unread item")
    sp_v.add_argument("--group", "-g", help="Restrict --next to a group")
    sp_v.add_argument("--raw", action="store_true", help="Show raw HTML instead of plain text")
    sp_v.add_argument("--mark-read", action="store_true", help="(Legacy) Mark item as read after viewing (default behavior)")
    sp_v.add_argument("--no-mark-read", action="store_true", help="Do not mark item as read after viewing")
    sp_v.set_defaults(func=cmd_view)

    sp = sub.add_parser("open", help="Open item link(s) in system browser")
    sp.add_argument("ids", type=int, nargs="+", help="Item ID(s)")
    sp.add_argument("--mark-read", action="store_true", help="Mark item(s) as read after opening")
    sp.set_defaults(func=cmd_open)

    # 'o' is a short alias of 'open'
    sp_o = sub.add_parser("o", help="Alias for 'open'")
    sp_o.add_argument("ids", type=int, nargs="+", help="Item ID(s)")
    sp_o.add_argument("--mark-read", action="store_true", help="Mark item(s) as read after opening")
    sp_o.set_defaults(func=cmd_open)

    sp = sub.add_parser("edit", help="Open item content in your editor")
    sp.add_argument("id", type=int, help="Item ID")
    sp.add_argument("--part", choices=["content", "summary", "title", "link", "url"], default="content", help="Which field to open (default: content)")
    sp.add_argument("--plain", action="store_true", help="Convert HTML to plain text for content/summary")
    sp.set_defaults(func=cmd_edit)

    sp = sub.add_parser("copy", help="Copy a field from one or more items to the system clipboard")
    sp.add_argument("ids", type=int, nargs="+", help="Item ID(s)")
    sp.add_argument("--part", choices=["title", "summary", "content", "link", "url"], help="Which field to copy (default: from config, else url)")
    sp.add_argument("--plain", action="store_true", help="Convert HTML to plain text for content/summary")
    sp.set_defaults(func=cmd_copy)

    # files sync: export a file tree for use with editors (e.g., nvim netrw)
    sp_files = sub.add_parser("files", help="Work with file-tree exports")
    subf = sp_files.add_subparsers(dest="files_cmd", required=True)
    sp_sync = subf.add_parser("sync", help="Sync items into a directory tree")
    sp_sync.add_argument("--dest", default=os.path.join(rssel_home(), "fs"), help="Destination directory (default: ./.rssel/fs)")
    sp_sync.add_argument("--group", "-g", help="Filter by group")
    sp_sync.add_argument("--unread-only", action="store_true", help="Only export unread items")
    sp_sync.add_argument("--limit", "-n", type=int, help="Limit number of items per group")
    sp_sync.add_argument("--format", choices=["md", "txt", "json", "html"], default="md", help="File format for content")
    sp_sync.add_argument("--clean", action="store_true", help="Clean dest directory before export")
    sp_sync.set_defaults(func=cmd_files_sync)

    # sync: fetch + export in one go (pivot to downloader)
    sp_all = sub.add_parser("sync", help="Fetch feeds and export grouped files")
    sp_all.add_argument("--group", "-g", help="Only process this group")
    sp_all.add_argument("--dest", default=os.path.join(rssel_home(), "fs"), help="Destination directory (default: ./.rssel/fs)")
    sp_all.add_argument("--format", choices=["md", "txt", "json", "html"], default="md", help="File format for content (default: md)")
    sp_all.add_argument("--unread-only", action="store_true", help="Only export unread items")
    sp_all.add_argument("--limit", "-n", type=int, help="Limit number of items per group")
    sp_all.add_argument("--clean", action="store_true", help="Clean dest directory before export")
    # Auto-tagging (enabled by default)
    sp_all.add_argument("--no-auto-tags", dest="auto_tags", action="store_false", help="Disable auto-tagging before export")
    sp_all.add_argument("--max-tags", type=int, default=5, help="Max tags per item when auto-tagging (default: 5)")
    sp_all.add_argument("--include-domain", action="store_true", help="Include site domain as a tag during auto-tagging")
    sp_all.set_defaults(auto_tags=True)
    sp_all.set_defaults(func=cmd_sync)

    # cold: archive filtered items into a tar(.gz) file
    sp_cold = sub.add_parser("cold", help="Cold storage: save filtered items into a tar or tar.gz")
    sp_cold.add_argument("--output", "-o", help="Output tar path ('.tar' or '.tar.gz'). Defaults to ./.rssel/cold-YYYYMMDDHHMMSS.tar.gz")
    sp_cold.add_argument("--no-gzip", action="store_true", help="Write plain .tar instead of .tar.gz (gzip is default)")
    sp_cold.add_argument("--format", choices=["md", "txt", "json", "html"], default="md", help="Export file format inside tar (default: md)")
    # Filters mirroring list
    sp_cold.add_argument("--group", "-g", help="Filter by group")
    sp_cold.add_argument("--tags", help="Comma/space separated tag names; item must have ALL")
    sp_cold.add_argument("--limit", "-n", type=int, help="Limit number of items")
    sp_cold.add_argument("--unread-only", action="store_true", help="Only show unread items")
    sp_cold.add_argument("--read", dest="read_only", action="store_true", help="Only show read items")
    sp_cold.add_argument("--star", dest="star_only", action="store_true", help="Only show starred (favorite) items")
    sp_cold.add_argument("--new", action="store_true", help="Only items added in the last 24 hours (config new_hours)")
    sp_cold.add_argument("--query", "-q", help="Search in title/summary")
    sp_cold.add_argument("--since", help="Filter by date/time >= (e.g., 2025-10-16 or 2025-10-16 12:00 or 'today')")
    sp_cold.add_argument("--until", help="Filter by date/time < (exclusive)")
    sp_cold.add_argument("--on", help="Filter items on a specific day (YYYY-MM-DD or 'today')")
    sp_cold.add_argument("--date-field", choices=["published", "created"], default="published", help="Which timestamp to use for date filters (default: published)")
    sp_cold.add_argument("--source", help="Filter by a source (url or numeric id)")
    sp_cold.add_argument("--highlight", action="store_true", help="Enable highlight evaluation (paired with --highlight-only)")
    sp_cold.add_argument("--highlight-only", action="store_true", help="Only include items that match the highlight word list")
    sp_cold.set_defaults(func=cmd_cold)

    # pick: fuzzy filter; outputs like list using show flags
    sp_pick = sub.add_parser("pick", help="Fuzzy-pick items (filters) and print them like list")
    sp_pick.add_argument("--group", "-g", help="Filter by group")
    sp_pick.add_argument("--tags", help="Comma/space separated tag names; item must have ALL")
    sp_pick.add_argument("--unread-only", action="store_true", help="Only include unread items")
    sp_pick.add_argument("--read", dest="read_only", action="store_true", help="Only include read items")
    sp_pick.add_argument("--star", dest="star_only", action="store_true", help="Only include starred (favorite) items")
    sp_pick.add_argument("--new", action="store_true", help="Only include items added in the last N hours (config new_hours)")
    sp_pick.add_argument("--limit", "-n", type=int, help="Limit number of items")
    sp_pick.add_argument("--since", help="Filter date/time >= (e.g., 2025-10-16 or 'today')")
    sp_pick.add_argument("--until", help="Filter date/time < (exclusive)")
    sp_pick.add_argument("--on", help="Filter items on a specific day (YYYY-MM-DD or 'today')")
    sp_pick.add_argument("--date-field", choices=["published", "created"], default="published")
    sp_pick.add_argument("--query", "-q", help="Initial search query for fzf and DB filter")
    # Sorting options similar to list
    sp_pick.add_argument("--sort-id", action="store_true")
    sp_pick.add_argument("--sort-id-rev", action="store_true")
    sp_pick.add_argument("--sort-name", action="store_true")
    sp_pick.add_argument("--sort-date-new", action="store_true")
    sp_pick.add_argument("--sort-date-old", action="store_true")
    sp_pick.add_argument("--sort-group", action="store_true")
    sp_pick.add_argument("--sort-count", action="store_true")
    sp_pick.add_argument("--no-fzf", action="store_true", help="Print directly without fzf; same as list")
    sp_pick.add_argument("--multi", action="store_true", help="Allow selecting multiple items in fzf")
    # Show options (same as list)
    sp_pick.add_argument("--show-url", action="store_true")
    sp_pick.add_argument("--show-tags", action="store_true")
    sp_pick.add_argument("--show-path", action="store_true")
    sp_pick.add_argument("--show-date", action="store_true")
    sp_pick.add_argument("--show-snippet", action="store_true")
    sp_pick.add_argument("--show-source", action="store_true")
    sp_pick.add_argument("--color", action="store_true")
    sp_pick.add_argument("--export", action="store_true", help="Export the filtered items to files")
    sp_pick.add_argument("--dest", help="Export destination directory (defaults to export_dir in config)")
    sp_pick.add_argument("--format", choices=["md", "txt", "json", "html"], help="Export format (defaults to export_format in config)")
    sp_pick.set_defaults(func=cmd_pick)

    # pick-tags: choose a tag via fzf/basic picker and show connected items
    sp_pt = sub.add_parser("pick-tags", help="Pick a tag (fzf if available) and print matching items like list")
    sp_pt.add_argument("--group", "-g", help="Filter tags/items by group")
    sp_pt.add_argument("--tags", help="Additional required tags (comma/space separated) for the second stage item filter")
    sp_pt.add_argument("--unread-only", action="store_true", help="Only include unread items")
    sp_pt.add_argument("--limit", "-n", type=int, help="Limit items per tag shown")
    sp_pt.add_argument("--no-fzf", action="store_true", help="Force basic picker without fzf")
    sp_pt.add_argument("--show-url", action="store_true", help="Show URL as indented metadata under each item")
    sp_pt.add_argument("--show-tags", action="store_true", help="Show tags as indented metadata under each item")
    sp_pt.add_argument("--show-path", action="store_true", help="Show expected file path as indented metadata under each item")
    sp_pt.add_argument("--show-date", action="store_true", help="Show published date as indented metadata under each item")
    sp_pt.add_argument("--show-snippet", action="store_true", help="Show a short text snippet under each item")
    sp_pt.add_argument("--preview", action="store_true", help="Show a preview of items for the highlighted tag in fzf")
    sp_pt.add_argument("--multi", action="store_true", help="Allow selecting multiple items in fzf stage")
    sp_pt.add_argument("--color", action="store_true", help="Colorize printed outputs")
    sp_pt.add_argument("--export", action="store_true", help="Export the filtered items to files")
    sp_pt.add_argument("--dest", help="Export destination directory (defaults to export_dir in config)")
    sp_pt.add_argument("--format", choices=["md", "txt", "json", "html"], help="Export format (defaults to export_format in config)")
    sp_pt.set_defaults(func=cmd_pick_tags)

    # preview helper for fzf
    sp_prev = sub.add_parser("preview", help="Internal: render item preview to stdout")
    sp_prev.add_argument("--id", type=int, required=True)
    sp_prev.add_argument("--width", type=int, help="Wrap width; defaults to terminal or FZF_PREVIEW_COLUMNS")
    sp_prev.set_defaults(func=cmd_preview)
    
    # config helpers
    sp_cfg = sub.add_parser("config", help="Config helpers")
    subc = sp_cfg.add_subparsers(dest="cfg_cmd", required=True)
    sp_ct = subc.add_parser("template", help="Print or write full config with defaults and comments")
    sp_ct.add_argument("--write", action="store_true", help="Write to the config path instead of printing")
    sp_ct.set_defaults(func=cmd_config_template)

    # tags: auto-generate and list
    sp_tags = sub.add_parser("tags", help="Tagging utilities")
    subt = sp_tags.add_subparsers(dest="tags_cmd", required=True)
    sp_auto = subt.add_parser("auto", help="Auto-generate tags for items")
    sp_auto.add_argument("--group", "-g", help="Only process this group")
    sp_auto.add_argument("--limit", "-n", type=int, help="Limit number of items")
    sp_auto.add_argument("--max-tags", type=int, default=5, help="Max tags per item (default: 5)")
    sp_auto.add_argument("--include-domain", action="store_true", help="Include site domain as a tag")
    sp_auto.add_argument("--dry-run", action="store_true", help="Show tags but do not save")
    sp_auto.set_defaults(func=cmd_tags_auto)

    sp_listtags = subt.add_parser("list", help="List tags with counts")
    sp_listtags.add_argument("--group", "-g", help="Filter by group")
    sp_listtags.add_argument("--sort", choices=["name","count-asc","count-desc"], help="Sort order (default: count-desc)")
    sp_listtags.add_argument("--top", type=int, help="Limit number of tags shown")
    sp_listtags.set_defaults(func=cmd_tags_list)

    sp_items = subt.add_parser("items", help="List items for a tag or tag set")
    sp_items.add_argument("--tag", required=True, help="Tag name (case-insensitive). Comma/space separated for ALL-match (e.g. 'us, trump')")
    sp_items.add_argument("--group", "-g", help="Filter by group")
    sp_items.add_argument("--limit", "-n", type=int, help="Limit items")
    sp_items.add_argument("--unread-only", action="store_true", help="Only unread items")
    sp_items.add_argument("--show-url", action="store_true", help="Show URL as indented metadata")
    sp_items.add_argument("--show-tags", action="store_true", help="Show tags as indented metadata")
    sp_items.add_argument("--show-path", action="store_true", help="Show expected file path as indented metadata")
    sp_items.add_argument("--show-date", action="store_true", help="Show published date as indented metadata")
    sp_items.add_argument("--show-snippet", action="store_true", help="Show a short text snippet under each item")
    sp_items.add_argument("--show-source", action="store_true", help="Show the source (title or URL) as an indented metadata line")
    sp_items.add_argument("--color", action="store_true", help="Colorize output")
    sp_items.add_argument("--nocolor", action="store_true", help="Disable ANSI colors in output (overrides --color and config)")
    sp_items.set_defaults(func=cmd_tags_items)

    sp_map = subt.add_parser("map", help="Show tags with their items")
    sp_map.add_argument("--group", "-g", help="Filter by group")
    sp_map.add_argument("--top", type=int, help="Show only the top N tags by count")
    sp_map.add_argument("--min-count", type=int, help="Only include tags with at least this many items")
    sp_map.add_argument("--sort", choices=["name","count-asc","count-desc"], help="Sort tags (default: count-desc)")
    sp_map.add_argument("--max-per-tag", type=int, default=10, help="Max items to list per tag (default: 10)")
    sp_map.add_argument("--compact", action="store_true", help="Compact Python-list style: tag (count): [id, ...]")
    sp_map.add_argument("--detailed", action="store_true", help="Show detailed lines with title/group/date")
    sp_map.add_argument("--color", action="store_true", help="Colorize printed outputs")
    sp_map.set_defaults(func=cmd_tags_map)

    # tags compact: all tags in a single line, super compact
    sp_cmap = subt.add_parser("compact", help="One-line compact tag map: tag(count): [ids]; ...")
    sp_cmap.add_argument("--group", "-g", help="Filter by group")
    sp_cmap.add_argument("--top", type=int, help="Show only the top N tags by count")
    sp_cmap.add_argument("--min-count", type=int, help="Only include tags with at least this many items")
    sp_cmap.add_argument("--sort", choices=["name","count-asc","count-desc"], help="Sort tags (default: count-desc)")
    sp_cmap.add_argument("--max-per-tag", type=int, default=10, help="Max items to include per tag (default: 10)")
    sp_cmap.add_argument("--color", action="store_true", help="Colorize tag names and counts")
    sp_cmap.set_defaults(func=cmd_tags_compact)

    # archive: mark items or feeds/groups archived (feeds archived also skipped in fetch)
    sp_arch = sub.add_parser("archive", help="Archive items, sources, or groups")
    suba = sp_arch.add_subparsers(dest="arch_cmd", required=True)
    sp_aid = suba.add_parser("id", help="Archive or unarchive by item id")
    sp_aid.add_argument("id", type=int)
    sp_aid.add_argument("--undo", action="store_true", help="Unarchive (restore)")
    sp_aid.set_defaults(func=cmd_archive_id)

    sp_asrc = suba.add_parser("source", help="Archive or unarchive a source (by URL or ID)")
    mx_src = sp_asrc.add_mutually_exclusive_group(required=True)
    mx_src.add_argument("--url")
    mx_src.add_argument("--id", type=int)
    sp_asrc.add_argument("--undo", action="store_true", help="Unarchive (include in fetch again)")
    sp_asrc.add_argument("--delete-items", action="store_true", help="Also mark all existing items from this source as deleted")
    sp_asrc.set_defaults(func=cmd_archive_source)

    sp_ag = suba.add_parser("group", help="Archive or unarchive all sources in a group")
    sp_ag.add_argument("--name", required=True)
    sp_ag.add_argument("--undo", action="store_true")
    sp_ag.add_argument("--delete-items", action="store_true")
    sp_ag.set_defaults(func=cmd_archive_group)

    # star: favorites with --undo for consistency
    sp_star = sub.add_parser("star", help="Star/unstar items (favorites)")
    sp_star.add_argument("ids", type=int, nargs="+", help="Item ID(s)")
    sp_star.add_argument("--undo", action="store_true", help="Unstar instead of star")
    sp_star.set_defaults(func=cmd_star)

    sp_ad = suba.add_parser("date", help="Archive or unarchive items by date range")
    sp_ad.add_argument("--since", help="Date/time >= (e.g., 2020-01-01 or 'today')")
    sp_ad.add_argument("--until", help="Date/time < (exclusive)")
    sp_ad.add_argument("--on", help="Archive items on a specific day (YYYY-MM-DD or 'today')")
    sp_ad.add_argument("--date-field", choices=["published", "created"], default="published", help="Which timestamp to use (default: published)")
    sp_ad.add_argument("--group", "-g", help="Filter by group(s), comma/space separated (OR)")
    sp_ad.add_argument("--source", help="Filter by source (url or numeric id)")
    sp_ad.add_argument("--undo", action="store_true", help="Unarchive (restore) instead of archive")
    sp_ad.set_defaults(func=cmd_archive_date)

    # delete: soft-delete items separate from archive
    sp_del = sub.add_parser("delete", help="Soft-delete items")
    subd = sp_del.add_subparsers(dest="del_cmd", required=True)
    sp_did = subd.add_parser("id", help="Delete or undelete item by id")
    sp_did.add_argument("id", type=int)
    sp_did.add_argument("--undo", action="store_true", help="Undelete instead of delete")
    sp_did.add_argument("--force", action="store_true", help="Allow deleting starred items")
    sp_did.set_defaults(func=cmd_delete_id)

    sp_dsrc = subd.add_parser("source", help="Delete or undelete items by source (by URL or ID)")
    mx_dsrc = sp_dsrc.add_mutually_exclusive_group(required=True)
    mx_dsrc.add_argument("--url")
    mx_dsrc.add_argument("--id", type=int)
    sp_dsrc.add_argument("--undo", action="store_true", help="Undelete instead of delete")
    sp_dsrc.add_argument("--force", action="store_true", help="Allow deleting starred items")
    sp_dsrc.set_defaults(func=cmd_delete_source)

    # purge: permanently delete data and vacuum
    sp_purge = sub.add_parser("purge", help="Permanently delete items and clean DB")
    sp_purge.add_argument("--deleted", action="store_true", help="Permanently remove items marked as deleted")
    sp_purge.add_argument("--before", help="Also remove read, non-starred items older than date/time (e.g., 2023-01-01 or 'yesterday')")
    sp_purge.add_argument("--older-days", type=int, help="Also remove read, non-starred items older than N days")
    sp_purge.add_argument("--group", "-g", help="Restrict purge to group(s) (comma/space separated)")
    sp_purge.add_argument("--source", help="Restrict purge to a source (url or numeric id)")
    sp_purge.add_argument("--source-id", type=int, help="Restrict purge to a source by numeric id (explicit)")
    sp_purge.add_argument("--clean-tags", action="store_true", help="Remove tags that no longer have any items")
    sp_purge.add_argument("--vacuum", action="store_true", help="VACUUM the database to reclaim disk space")
    sp_purge.add_argument("--dry-run", action="store_true", help="Show what would be removed without deleting")
    sp_purge.set_defaults(func=cmd_purge)

    # purge shortcuts
    sp_pdel = sub.add_parser("purge-deleted", help="Shortcut: purge deleted items (optional: scope and cleanup)")
    sp_pdel.add_argument("--group", "-g", help="Restrict to group(s)")
    sp_pdel.add_argument("--source", help="Restrict to a source (url or numeric id)")
    sp_pdel.add_argument("--source-id", type=int, help="Restrict to a source by numeric id")
    sp_pdel.add_argument("--clean-tags", action="store_true")
    sp_pdel.add_argument("--vacuum", action="store_true")
    sp_pdel.add_argument("--dry-run", action="store_true")
    sp_pdel.set_defaults(func=lambda a: cmd_purge(argparse.Namespace(deleted=True, before=None, older_days=None, group=a.group, source=a.source, source_id=a.source_id, clean_tags=a.clean_tags, vacuum=a.vacuum, dry_run=a.dry_run)))

    # source management
    sp_src = sub.add_parser("source", help="Manage sources (DB)")
    subs = sp_src.add_subparsers(dest="source_cmd", required=True)
    sp_rm = subs.add_parser("rm", help="Remove a source (and its items) from the DB")
    mx_rm = sp_rm.add_mutually_exclusive_group(required=True)
    mx_rm.add_argument("--url")
    mx_rm.add_argument("--id", type=int)
    sp_rm.add_argument("--yes", action="store_true", help="Confirm removal (non-interactive)")
    sp_rm.add_argument("--vacuum", action="store_true", help="VACUUM after removal")
    sp_rm.set_defaults(func=cmd_source_rm)

    # --- Simple aliases for common flows ---
    # a: archive source by id/url
    sp_a = sub.add_parser("a", help="Alias: archive source by id/url (use --id or --url)")
    mx_a = sp_a.add_mutually_exclusive_group(required=True)
    mx_a.add_argument("--id", type=int)
    mx_a.add_argument("--url")
    sp_a.add_argument("--undo", action="store_true")
    sp_a.add_argument("--delete-items", action="store_true")
    sp_a.set_defaults(func=cmd_archive_source)

    # rm: remove source from DB by id/url
    sp_rm2 = sub.add_parser("rm", help="Alias: remove a source from DB (and its items)")
    mx_rm2 = sp_rm2.add_mutually_exclusive_group(required=True)
    mx_rm2.add_argument("--id", type=int)
    mx_rm2.add_argument("--url")
    sp_rm2.add_argument("--yes", action="store_true")
    sp_rm2.add_argument("--vacuum", action="store_true")
    sp_rm2.set_defaults(func=cmd_source_rm)

    # trash: soft-delete items (alias of delete)
    sp_tr = sub.add_parser("trash", help="Alias: soft-delete items")
    subtr = sp_tr.add_subparsers(dest="trash_cmd", required=True)
    sp_trid = subtr.add_parser("id", help="Delete or undelete item by id")
    sp_trid.add_argument("id", type=int)
    sp_trid.add_argument("--undo", action="store_true")
    sp_trid.add_argument("--force", action="store_true")
    sp_trid.set_defaults(func=cmd_delete_id)
    sp_trsrc = subtr.add_parser("source", help="Delete or undelete items by source")
    mx_trs = sp_trsrc.add_mutually_exclusive_group(required=True)
    mx_trs.add_argument("--id", type=int)
    mx_trs.add_argument("--url")
    sp_trsrc.add_argument("--undo", action="store_true")
    sp_trsrc.add_argument("--force", action="store_true")
    sp_trsrc.set_defaults(func=cmd_delete_source)

    # pd: purge-deleted shortcut
    sp_pd = sub.add_parser("pd", help="Alias: purge deleted items (optional: scope and cleanup)")
    sp_pd.add_argument("--group", "-g")
    sp_pd.add_argument("--source")
    sp_pd.add_argument("--source-id", type=int)
    sp_pd.add_argument("--clean-tags", action="store_true")
    sp_pd.add_argument("--vacuum", action="store_true")
    sp_pd.add_argument("--dry-run", action="store_true")
    sp_pd.set_defaults(func=lambda a: cmd_purge(argparse.Namespace(deleted=True, before=None, older_days=None, group=a.group, source=a.source, source_id=a.source_id, clean_tags=a.clean_tags, vacuum=a.vacuum, dry_run=a.dry_run)))

    return p


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
        sys.exit(rc or 0)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
