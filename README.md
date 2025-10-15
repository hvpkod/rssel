# rssel
Lightweight CLI to fetch RSS/Atom feeds and store articles on disk, grouped by source, with clean Markdown by default.

Quick start
- Run with Python 3.10+: `python3 rssel.py <command>`
- Initialize config and example sources: `python3 rssel.py init`
- Inspect sources: `python3 rssel.py sources`
- One-shot sync (fetch + export to files):
  - `python3 rssel.py sync --dest ./.rssel/fs --format md` (default)
  - Group only: `python3 rssel.py sync --group tech`
  - Other formats: `--format txt|json|html`

Features
- Grouped sources in a simple TOML file
- Fetch and cache items in a local SQLite DB
- Sync to a file tree per group; default is cleaned-up Markdown
- Format options: `md` (default), `txt`, `json`, `html`
- Optional interactive commands still available (list/view/open/export)
- Auto-tagging: generate tags from article text and list popular tags

Config
- Self-contained: stored under `./.rssel/` (auto-created by `init`)
- Files: `./.rssel/config.toml`, `./.rssel/sources.toml`, `./.rssel/data.sqlite`
- Keys: `data_dir`, `sources_file`, `stopwords_file`, `editor`, `clipboard_cmd`
- Override base dir with `RSSEL_HOME=/path/to/dir`

Sources
- Location: `./.rssel/sources.toml`
- TOML format:
  
  ```toml
  [groups]
  tech = ["https://this-week-in-rust.org/rss.xml"]
  news = ["https://www.theguardian.com/world/rss"]
  ```

CLI
- `rssel init` — create config and example sources
- `rssel sources` — show configured groups and feeds
- `rssel sync [--dest DIR] [--group <name>] [--unread-only] [--limit N] [--format md|txt|json|html] [--clean]` — fetch and export grouped files (default: cleaned Markdown)
- `rssel tags auto [--group <name>] [--limit N] [--max-tags K] [--include-domain] [--dry-run]` — auto-generate and save tags for items
- `rssel tags list [--group <name>]` — list tags with counts
- `rssel tags items --tag <name|"a, b"> [--group <name>] [--limit N] [--unread-only] [--show-url] [--show-tags] [--show-path] [--show-snippet]` — list items connected to a single tag or an ALL-match set (comma/space separated); add indented lines and optional snippet
  - Show order follows the order you pass flags.
- `rssel tags map [--group <name>] [--top N] [--max-per-tag M]` — show tags and associated items
- `rssel pick-tags [--group <name>] [--tags "a, b"] [--unread-only] [--limit N] [--no-fzf] [--show-url] [--show-tags] [--show-path] [--show-snippet] [--preview] [--part content|summary|title|link]` — pick a tag (fzf if available) and show connected articles; second-stage fzf lets you pick an item with preview and actions (Enter=view, Ctrl-O=open, Ctrl-E=editor, Ctrl-Y=copy, Ctrl-M=mark read, Ctrl-U=mark unread). `--tags` adds extra required tags for the second stage filter.
- `rssel fetch [--group <name>]` — fetch feeds and cache
- `rssel files sync [--dest DIR] [--group <name>] [--unread-only] [--limit N] [--format md|txt|json|html] [--clean]` — export items to a file tree
- `rssel list [--group <name>] [--tags "a, b"] [--limit N] [--unread-only] [--query Q] [--show-url] [--show-tags] [--show-path] [--show-snippet]` — filter by multiple tags (ALL must match) and add indented lines for URL, tags, path, date, optional snippet
  - Show order follows the order you pass flags (e.g., `--show-date --show-url` prints date before url). Default order without flags: url, tags, path, date, snippet.
- `rssel export <id> [--part title|summary|content|link] [--to stdout|editor|clipboard]` (add `--plain` for text)
- `rssel mark <id> read|unread`
- `rssel view [<id>] [--next] [--group <name>] [--raw] [--mark-read]`
- `rssel open <id> [--mark-read]`
- `rssel pick` — fuzzy filter (fzf) over items; prints results like `list`. Use the same filter and `--show-*` flags as `list`. Add `--multi` to select multiple items.
 

Notes
- Clipboard export auto-detects `wl-copy`, `xclip`, `xsel`, `pbcopy`, or use `clipboard_cmd` in config.
- Editor export uses `$RSSEL_EDITOR`, `$EDITOR`, or `editor` from config, default `nvim`.
- Reader uses `$RSSEL_PAGER`/`$PAGER` or falls back to `less`/`more`.
- File tree creates `./.rssel/fs/<group>/<id>-<slug>.<ext>` (`md` default, or `txt`/`json`/`html`).
  - Markdown/Text exports convert article HTML to clean plain text for readability and add a simple header (including tags if present).
- Picker uses `fzf` if installed (override with `RSSEL_PICKER`); falls back to a simple numbered prompt.

Tagging
- Stopwords file: edit `./.rssel/stopwords.txt` to exclude overly common or irrelevant words from tags.
- Format: one word per line; `#` starts a comment; case-insensitive. Example entries: `talk`, `believe`, `and`, `row`, `six`.

Display defaults
- Set defaults in `./.rssel/config.toml`:
  - `display_color = "true|false"`
  - `display_show_url|_tags|_path|_date|_snippet = "true|false"`
  - `display_snippet_len = "240"` (characters)
 
