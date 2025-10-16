#!/usr/bin/env python3
import argparse
import os
import sys
import sqlite3
import json
import shutil
import tempfile
import subprocess
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
        "display_show_tags = \"true\"\n"
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
        "clipboard_cmd = \"\"\n"
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
    return {
        "show_url": opt("show_url"),
        "show_tags": opt("show_tags"),
        "show_path": opt("show_path"),
        "show_date": opt("show_date"),
        "show_snippet": opt("show_snippet"),
        "show_source": opt("show_source"),
        "color": opt("color"),
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


def parse_basic_toml_map_array(text: str) -> dict:
    # Legacy support removed in favor of JSON sources; keep stub to avoid breakage
    return {}


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
        "export_dir": os.path.join(rssel_home(), "fs"),
        "export_format": "md",
        "external_export_dir": None,
        "external_export_format": "md",
        "new_hours": "24",
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
    for grp in sorted(mapping.keys()):
        items = mapping[grp]
        print(f"[{grp}] ({len(items)})")
        for (title, url) in items:
            if title:
                print(f"  - {title} — {url}")
            else:
                print(f"  - {url}")
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
    conn = db_conn()
    init_db(conn)
    cur = conn.cursor()
    # If --source present with no value, list sources summary
    if getattr(args, "source", None) == "__LIST__":
        cur.execute("SELECT rowid, url, COALESCE(title, url) as name, archived FROM feeds ORDER BY name COLLATE NOCASE")
        feeds_rows = cur.fetchall()
        for (rid, url, name, archived) in feeds_rows:
            # Count items
            cur.execute("SELECT COUNT(*) FROM items WHERE feed_url = ? AND deleted = 0", (url,))
            count = (cur.fetchone() or (0,))[0]
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
                LIMIT 10
                """,
                (url,),
            )
            tag_list = [r[0] for r in cur.fetchall()]
            arch = " [archived]" if archived else ""
            print(f"{rid:4d}  {name}{arch}  (items: {count})  tags: [" + ", ".join(tag_list) + "]")
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
    if src and src != "__LIST__":
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
    limit_sql = f"LIMIT {int(args.limit)}" if args.limit else ""
    sql = f"""
        SELECT items.id, items.published_ts, items.read, feeds.grp, items.title, items.feed_url
        FROM items JOIN feeds ON items.feed_url = feeds.url
        {where_sql}
        ORDER BY items.published_ts DESC, items.id DESC
        {limit_sql}
    """
    cur.execute(sql, params)
    rows = cur.fetchall()
    opts = resolve_display_opts(args)
    # Optional export
    if getattr(args, "export", False) and rows:
        cfg = read_config()
        dest = args.dest or cfg.get("export_dir") or os.path.join(rssel_home(), "fs")
        fmt = args.format or cfg.get("export_format") or "md"
        n = export_rows(conn, rows, os.path.abspath(dest), fmt)
        print(f"Exported {n} items to {os.path.abspath(dest)} (format: {fmt})")
    # Pre-prepare a cursor for fetching groups per feed
    cur_groups = conn.cursor()
    for (iid, ts, read, grp, title, feed_url) in rows:
        mark = " " if read else "*"
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
        if opts["show_url"] or opts["show_tags"] or opts["show_path"] or opts["show_snippet"] or opts["show_date"]:
            id_s = _maybe(str(iid), opts["color"], 2)  # dim
            mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
            grp_s = _maybe(grp_label, opts["color"], 36)  # cyan
            title_s = _maybe(title or '', opts["color"], 1)
            print(f"{id_s} {mark_s} {grp_s} {title_s}")
        else:
            id_s = _maybe(f"{iid:6d}", opts["color"], 2)
            mark_s = _maybe(mark, opts["color"] and mark == "*", 33, 1) if mark.strip() else mark
            grp_s = _maybe(grp_label, opts["color"], 36)
            dt_s = _maybe(dt, opts["color"], 2)
            title_s = _maybe(title or '', opts["color"], 1)
            print(f"{id_s} {mark_s} {grp_s} {dt_s}  {title_s}")
        if opts.get("show_url") or opts.get("show_tags") or opts.get("show_path") or opts.get("show_snippet") or opts.get("show_date") or opts.get("show_source") or getattr(args, "show_source", False):
            # pass feed_url in fallback so source lookup works
            item = get_item(conn, iid) or {"id": iid, "link": None, "group": grp, "title": title, "feed_url": feed_url}
            # Merge show_source flag from args into opts for order handling
            if getattr(args, "show_source", False):
                opts = dict(opts)
                opts["show_source"] = True
            _print_meta_block(conn, item, dt, opts)
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


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    # keep letters, numbers, and hyphens inside words; split on others
    words = re.split(r"[^a-z0-9\-]+", text)
    return [w.strip("-") for w in words if w and not w.isdigit()]


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
    if args.undo:
        cur.execute("UPDATE feeds SET archived = 0 WHERE url = ?", (args.url,))
        print(f"Unarchived source {args.url}")
    else:
        cur.execute("UPDATE feeds SET archived = 1 WHERE url = ?", (args.url,))
        print(f"Archived source {args.url}")
        if args.delete_items:
            cur.execute("UPDATE items SET deleted = 1 WHERE feed_url = ? AND starred = 0", (args.url,))
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
    rows = cur.fetchall()
    for name, cnt in rows:
        print(f"{cnt:5d}  {name}")
    return 0


def _print_item_rows(rows):
    for (iid, ts, read, grp, title) in rows:
        mark = " " if read else "*"
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
        print(f"{iid:6d} {mark} [{grp}] {dt}  {title or ''}")


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
    sql_tags = f"""
        SELECT t.id, t.name, COUNT(*) as cnt
        FROM tags t
        JOIN item_tags it ON t.id = it.tag_id
        JOIN items ON items.id = it.item_id
        JOIN feeds ON feeds.url = items.feed_url
        {where_sql}
        GROUP BY t.id, t.name
        ORDER BY cnt DESC, t.name
        {top_limit}
    """
    cur.execute(sql_tags, params)
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
            print(f"{name} ({cnt}): [" + ", ".join(str(i) for i in ids) + "]")
        else:
            print(f"[{name}] ({cnt})")
            for (iid, ts, read, grp, title) in rows:
                mark = " " if read else "*"
                dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
                print(f"  {iid:6d} {mark} [{grp}] {dt}  {title or ''}")
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
    top_limit = f"LIMIT {int(args.top)}" if args.top else ""
    sql_tags = f"""
        SELECT t.id, t.name, COUNT(*) as cnt
        FROM tags t
        JOIN item_tags it ON t.id = it.tag_id
        JOIN items ON items.id = it.item_id
        JOIN feeds ON feeds.url = items.feed_url
        {where_sql}
        GROUP BY t.id, t.name
        ORDER BY cnt DESC, t.name
        {top_limit}
    """
    cur.execute(sql_tags, params)
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


def query_items(conn: sqlite3.Connection, group: str | None, unread_only: bool, limit: int | None, query: str | None):
    cur = conn.cursor()
    where = []
    params: list = []
    where.append("items.deleted = 0")
    if group:
        glist = _parse_groups_arg(group)
        if glist:
            placeholders = ",".join(["?"] * len(glist))
            where.append(f"EXISTS (SELECT 1 FROM feed_groups fg WHERE fg.url = items.feed_url AND fg.grp IN ({placeholders}))")
            params.extend(glist)
    if unread_only:
        where.append("items.read = 0")
    if query:
        where.append("(items.title LIKE ? OR items.summary LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    sql = f"""
        SELECT items.id, items.published_ts, items.read, feeds.grp, items.title, items.starred
        FROM items JOIN feeds ON items.feed_url = feeds.url
        {where_sql}
        ORDER BY items.published_ts DESC, items.id DESC
        {limit_sql}
    """
    cur.execute(sql, params)
    return cur.fetchall()


def make_pick_lines(rows):
    # Tab-separated to simplify parsing. id is the first column.
    out = []
    for (iid, ts, read, grp, title, *rest) in rows:
        # If starred column is included by caller, rest[0] may be starred
        starred = None
        if rest:
            starred = rest[0]
        mark = (" " if read else "*") + ("★" if starred else " ")
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "----"
        out.append(f"{iid}\t{mark}\t[{grp}]\t{dt}\t{title or ''}")
    return "\n".join(out)


def pick_with_fzf(lines: str, initial_query: str | None):
    cmd = detect_fzf()
    if not cmd:
        return None
    fzf_cmd = cmd + ["--with-nth=1..", "--prompt", "rssel> "]
    if initial_query:
        fzf_cmd += ["--query", initial_query]
    proc = subprocess.Popen(fzf_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = proc.communicate(input=lines.encode("utf-8", errors="replace"))
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", errors="replace").strip()


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
        limit_sql = f"LIMIT {int(args.limit)}" if args.limit else ""
        sql = f"""
            SELECT items.id, items.published_ts, items.read, feeds.grp, items.title, items.summary
            FROM items JOIN feeds ON items.feed_url = feeds.url
            {where_sql}
            ORDER BY items.published_ts DESC, items.id DESC
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


def perform_pick_action(conn: sqlite3.Connection, iid: int, args):
    if args.action == "print-id":
        print(iid)
        return 0
    elif args.action == "view":
        text = format_item_for_reading(get_item(conn, iid), mode="plain")
        run_pager(text)
        if getattr(args, "mark_read", False):
            cur = conn.cursor()
            cur.execute("UPDATE items SET read = 1 WHERE id = ?", (iid,))
            conn.commit()
        return 0
    elif args.action == "open":
        return cmd_open(argparse.Namespace(id=iid, mark_read=getattr(args, "mark_read", False)))
    elif args.action == "export-editor":
        item = get_item(conn, iid)
        if not item:
            return 1
        text = item.get(args.part) or item.get("content") or item.get("summary") or item.get("link") or ""
        if args.part in ("content", "summary"):
            text = html_to_text(text)
        return export_to_editor(text, read_config())
    elif args.action == "export-clipboard":
        item = get_item(conn, iid)
        if not item:
            return 1
        text = item.get(args.part) or item.get("content") or item.get("summary") or item.get("link") or ""
        if args.part in ("content", "summary"):
            text = html_to_text(text)
        return export_to_clipboard(text, read_config())
    elif args.action == "pipe":
        item = get_item(conn, iid)
        if not item:
            return 1
        default = read_config().get("pipe_cmd") or "nvim -R -"
        # non-interactive: just use default here
        text = item.get(args.part) or item.get("content") or item.get("summary") or item.get("link") or ""
        return pipe_to_command(text, default)
    elif args.action == "mark-read":
        cur = conn.cursor()
        cur.execute("UPDATE items SET read = 1 WHERE id = ?", (iid,))
        conn.commit()
        print(f"Marked {iid} as read")
        return 0
    elif args.action == "mark-unread":
        cur = conn.cursor()
        cur.execute("UPDATE items SET read = 0 WHERE id = ?", (iid,))
        conn.commit()
        print(f"Marked {iid} as unread")
        return 0
    elif args.action == "toggle-star":
        cur = conn.cursor()
        cur.execute("UPDATE items SET starred = 1 - COALESCE(starred, 0) WHERE id = ?", (iid,))
        conn.commit()
        return 0
    elif args.action == "trash":
        cur = conn.cursor()
        cur.execute("UPDATE items SET deleted = 1 WHERE id = ?", (iid,))
        conn.commit()
        print(f"Trashed {iid}")
        return 0
    elif args.action == "delete":
        # Permanent delete
        cur = conn.cursor()
        cur.execute("DELETE FROM items WHERE id = ?", (iid,))
        conn.commit()
        print(f"Deleted {iid}")
        return 0
    elif args.action == "reload":
        # Re-fetch current group (if any) and return
        cmd_fetch(argparse.Namespace(group=getattr(args, "group", None)))
        return 0
    elif args.action == "cycle-group":
        # Move the item's feed to the next group from sources
        item = get_item(conn, iid)
        if not item:
            return 1
        src = read_sources()
        all_groups = sorted(src.keys())
        if not all_groups:
            return 0
        cur_grp = item.get("group")
        try:
            idx = all_groups.index(cur_grp)
        except ValueError:
            idx = -1
        next_grp = all_groups[(idx + 1) % len(all_groups)]
        cur = conn.cursor()
        cur.execute("UPDATE feeds SET grp = ? WHERE url = ?", (next_grp, item.get("feed_url")))
        conn.commit()
        print(f"Moved feed to group: {next_grp}")
        return 0
    else:
        return 1


def build_parser():
    p = argparse.ArgumentParser(prog=APP_NAME, description="Lightweight, self-contained CLI RSS reader")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="Create local config, sources, and DB")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("sources", help="Show configured groups and feeds")
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
    sp.add_argument("--source", nargs="?", const="__LIST__", help="Filter by a source (url or numeric id). If used with no value, lists sources summary")
    sp.add_argument("--list-tags", action="store_true", help="List all tags with counts (optional: filter by --group)")
    sp.add_argument("--show-url", action="store_true", help="Show URL as an indented metadata line")
    sp.add_argument("--show-tags", action="store_true", help="Show tags as an indented metadata line")
    sp.add_argument("--show-path", action="store_true", help="Show expected file path as an indented metadata line")
    sp.add_argument("--show-date", action="store_true", help="Show published date as an indented metadata line")
    sp.add_argument("--show-snippet", action="store_true", help="Show a short text snippet under each item")
    sp.add_argument("--show-source", action="store_true", help="Show the source (title or URL) as an indented metadata line")
    sp.add_argument("--color", action="store_true", help="Colorize output (titles, groups, markers)")
    sp.add_argument("--export", action="store_true", help="Export the filtered items to files")
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

    sp = sub.add_parser("open", help="Open item link(s) in system browser")
    sp.add_argument("ids", type=int, nargs="+", help="Item ID(s)")
    sp.add_argument("--mark-read", action="store_true", help="Mark item(s) as read after opening")
    sp.set_defaults(func=cmd_open)

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
    sp_items.set_defaults(func=cmd_tags_items)

    sp_map = subt.add_parser("map", help="Show tags with their items")
    sp_map.add_argument("--group", "-g", help="Filter by group")
    sp_map.add_argument("--top", type=int, help="Show only the top N tags by count")
    sp_map.add_argument("--max-per-tag", type=int, default=10, help="Max items to list per tag (default: 10)")
    sp_map.add_argument("--compact", action="store_true", help="Compact Python-list style: tag (count): [id, ...]")
    sp_map.add_argument("--detailed", action="store_true", help="Show detailed lines with title/group/date")
    sp_map.set_defaults(func=cmd_tags_map)

    # tags compact: all tags in a single line, super compact
    sp_cmap = subt.add_parser("compact", help="One-line compact tag map: tag(count): [ids]; ...")
    sp_cmap.add_argument("--group", "-g", help="Filter by group")
    sp_cmap.add_argument("--top", type=int, help="Show only the top N tags by count")
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

    sp_asrc = suba.add_parser("source", help="Archive or unarchive a source (by URL)")
    sp_asrc.add_argument("--url", required=True)
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
