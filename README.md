# rssel
Lightweight CLI to fetch RSS/Atom feeds, tag and star favorites, and store articles on disk, grouped by source. JSON sources with titles + multi-group support. Clean Markdown export by default.

Quick start
- Run with Python 3.10+: `python3 rssel.py <command>`
- Initialize: `python3 rssel.py init` (creates config + JSON sources)
- Show sources: `python3 rssel.py sources`
- Fetch + export (auto-tags by default): `python3 rssel.py sync`
  - Group filter (OR): `python3 rssel.py sync --group "tech, ideas"`
  - Formats: `--format md|txt|json|html`

Features
- JSON sources with titles + multi-group membership
- Fetch + cache in SQLite; auto-tag on sync (configurable)
- Export to project file tree (`./.rssel/fs`); default Markdown cleaned up from HTML
- Formats: `md` (default), `txt`, `json`, `html`
- List/pick with rich filters (tags, date ranges, groups OR, source, read/unread/star)
- Stars (favorites) protected from archive until unstarred
- Archive by id/source/group/date window
- Tagging utilities + compact tag maps

Config
- Self-contained under `./.rssel/` (created by `init`)
- Files: `./.rssel/config.toml`, `./.rssel/sources.json`, `./.rssel/data.sqlite`, `./.rssel/stopwords.txt`
- Generate a full config (with all options + comments):
  - Print: `python3 rssel.py config template`
  - Write: `python3 rssel.py config template --write`
- Key highlights
  - Paths: `data_dir`, `sources_file`, `stopwords_file`
  - Display defaults: `display_*` + `display_snippet_len`
  - Internal export (sync/list --export): `export_dir`, `export_format`
  - External export (export --to file): `external_export_dir`, `external_export_format`
  - New items window: `new_hours`
  - Copy defaults: `copy_default_part`, `copy_default_plain`, `copy_separator`
  - Sync auto-tagging: `sync_auto_tags`, `sync_max_tags`, `sync_include_domain`
  - Tools: `editor`, `clipboard_cmd`

Sources (JSON)
- Location: `./.rssel/sources.json`
- Shape:
  ```json
  {
    "sources": [
      { "title": "Simon Sinek", "url": "https://simonsinek.com/feed/", "groups": ["ideas","tech"] },
      { "title": "This Week in Rust", "url": "https://this-week-in-rust.org/rss.xml", "groups": ["tech"] }
    ]
  }
  ```

CLI (highlights)
- `rssel init` — create config and example sources
- `rssel sources` — show sources grouped
- `rssel sync [--group Gs] [--dest DIR] [--format md|txt|json|html] [--clean] [--no-auto-tags] [--max-tags K] [--include-domain]` — fetch + auto-tag + export
- `rssel fetch [--group Gs]` — fetch only
- `rssel list [--group Gs] [--tags T1,T2] [--limit N] [--unread-only|--read|--star] [--query Q]
              [--since D|--until D|--on D] [--date-field published|created]
              [--source [SRC]] [--show-source|--show-url|--show-tags|--show-path|--show-date|--show-snippet] [--color]` — filter and print items
  - `--group Gs` accepts comma/space-separated groups (OR)
  - `--source` with no value prints a sources summary (id, name, item count, top tags)
  - Show order follows the order you pass flags
- `rssel pick` — fuzzy filter with the same filters as list; Enter prints all matches
- `rssel tags auto|list|items|map|compact` — tagging utilities; `compact` prints all tags in one line
- `rssel view [<id>] [--next] [--group G] [--raw] [--no-mark-read]` — view in pager (marks read by default; opt out with `--no-mark-read`)
- `rssel open <ids...> [--mark-read]` — open link(s) in browser (one tab per id)
- `rssel export <ids...> --to file [--dest DIR] [--format md|txt|json|html]` — export outside project tree
- `rssel copy <ids...> [--part url|title|summary|content] [--plain]` — copy fields to clipboard (respects config defaults)
- `rssel star <ids...> [--undo]` — star/unstar favorites (starred cannot be archived)
- `rssel archive id <id> [--undo]` — archive/unarchive one item (refuses on starred)
- `rssel archive source --url <feed> [--undo] [--delete-items]` — archive/unarchive a source (skip starred on delete)
- `rssel archive group --name G [--undo] [--delete-items]` — archive/unarchive all sources in G (skip starred on delete)
- `rssel archive date [--since D] [--until D] [--on D] [--date-field published|created] [--group Gs] [--source SRC] [--undo]` — archive window (skip starred)
 

Notes
- Clipboard export auto-detects `wl-copy`, `xclip`, `xsel`, `pbcopy`, or use `clipboard_cmd` in config.
- Editor export uses `$RSSEL_EDITOR`, `$EDITOR`, or `editor` from config, default `nvim`.
- Reader uses `$RSSEL_PAGER`/`$PAGER` or falls back to `less`/`more`.
- File tree creates `./.rssel/fs/<group>/<id>-<slug>.<ext>`; Markdown/Text are cleaned to plain text for readability, with a simple header (title/date/link/tags).
- Picker uses `fzf` if installed (override with `RSSEL_PICKER`); falls back to a simple prompt.

Tagging
- Stopwords: edit `./.rssel/stopwords.txt` (one word per line; `#` starts a comment; case-insensitive).
- Auto-tagging runs on sync by default (configurable), or run `tags auto` manually.

Display defaults
- Set defaults in `./.rssel/config.toml`:
  - `display_color = "true|false"`
  - `display_show_url|_tags|_path|_date|_snippet|_source = "true|false"`
  - `display_snippet_len = "240"`
 
