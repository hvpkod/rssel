# rssel
Lightweight CLI for RSS/Atom: fetch, tag, star, and export. Uses JSON sources with titles and multi‑group support. Clean Markdown export by default.

Quick Start
- Python 3.10+: `python3 rssel.py <command>`
- Initialize: `python3 rssel.py init`
- Show sources: `python3 rssel.py sources`
- Fetch + export (auto‑tags by default): `python3 rssel.py sync`
  - Group filter (OR): `--group "tech, ideas"`
  - Formats: `--format md|txt|json|html`

Features
- JSON sources with titles and multi‑group membership
- SQLite cache; optional auto‑tag on sync
- File‑tree export to `./.rssel/fs` (Markdown, Text, JSON, HTML)
- Powerful filters: groups, tags, date windows, source, read/unread/star, query
- Stars (favorites) protected from archival purge
- Archive by id/source/group/date window
- Highlight word list to mark or filter matching items
- Purge deleted/old items and VACUUM to reclaim disk

Configuration
- Lives under `./.rssel/` (created by `init`)
- Files: `config.toml`, `sources.json`, `data.sqlite`, `stopwords.txt`, `highlights.txt`
- Generate a full config with comments:
  - Print: `python3 rssel.py config template`
  - Write: `python3 rssel.py config template --write`
- Notable options
  - Paths: `data_dir`, `sources_file`, `stopwords_file`, `highlight_words_file`
  - Display: `display_*`, `display_snippet_len`, `display_show_code`, `display_grid`, `display_json`, `display_highlight`, `display_highlight_only`
  - Internal export: `export_dir`, `export_format`
  - External export: `external_export_dir`, `external_export_format`
  - Sync writing: `sync_write_files` (true/false) to control whether `sync` writes files; override per‑run with `sync --write-file`
  - New items window: `new_hours`
  - Copy defaults: `copy_default_part`, `copy_default_plain`, `copy_separator`
  - Auto‑tagging: `sync_auto_tags`, `sync_max_tags`, `sync_include_domain`
  - Tools: `editor`, `clipboard_cmd`

Sources (JSON)
- Path: `./.rssel/sources.json`
- Example:
  ```json
  {
    "sources": [
      { "title": "Simon Sinek", "url": "https://simonsinek.com/feed/", "groups": ["ideas","tech"], "tier": 2 },
      { "title": "This Week in Rust", "url": "https://this-week-in-rust.org/rss.xml", "groups": ["tech"], "tier": 1 }
    ]
  }
  ```

Key Commands
- `rssel init` — create config and example sources
- `rssel sources` — show sources by group
  - Add `--with-db` to include DB status: archived flag and current item counts
  - Add `--include-db-only` with `--with-db` to also list DB-only sources not present in the config (useful after purges or config edits)
- `rssel sync` — fetch + auto‑tag + export
  - `--group Gs` `--dest DIR` `--format md|txt|json|html` `--clean` `--no-auto-tags` `--max-tags K` `--include-domain` `--write-file`
  - Controlled by `sync_write_files` in config; use `--write-file` to force enabling for a run
- `rssel fetch [--group Gs]` — fetch only
  - Filter by tier: `--tier 1,2,3`
- `rssel list` — filter and print items
  - Filters: `--group Gs` `--tags T1,T2` `--limit N` `--unread-only|--read|--star` `--query Q`
  - Dates: `--since D` `--until D` `--on D` `--date-field published|created` (aliases: `--from`, `--to`)
  - Source: `--source URL|ID` to filter; `--sources` (or `--list-sources`) to list a summary with ids
  - Sort: `--sort-id` (id desc), `--sort-id-rev` (id asc), `--sort-name` (title A→Z), `--sort-group` (group A→Z, then newest), `--sort-count` (by tag count desc), `--sort-date-new` (newest), `--sort-date-old` (oldest)
  - Show (non‑grid): `--show-source|--show-url|--show-tags|--show-path|--show-date|--show-snippet`
    - Suppress tags even if enabled in config: `--no-show-tags`
  - Grid: `--grid` prints a base row (id, markers, groups, title). To include extra metadata rows in grid, use `--grid-meta date,path,url,tags,source,snippet` (comma or space list). Each selected meta prints as `id  label: value` on its own row.
  - JSON: `--json` outputs a JSON array. By default includes base fields (id, title, read, published_ts, primary_group, groups, feed_url, highlight). Optional fields (date, source, link, path, tags, snippet) are included only if you also pass the corresponding `--show-*` flags. JSON is plain (no ANSI colors). Short codes are not included in JSON.
  - Color: `--color` enables ANSI colors; use `--nocolor` to disable
  - Highlight: `--highlight` (marks matches with `!`), `--highlight-only` (filter to matches)
- `rssel pick` — fuzzy filter (fzf if available) with same filters as list; supports `--group-by date|group|tier|tag|source` and grid/meta/highlight/export
-- `rssel tags auto|list|items|map|compact` — tagging tools
  - `tags list`: supports `--group`, `--source`, `--sort name|count-asc|count-desc` (default: count-desc), `--top N`
  - `tags map`: supports `--group`, `--source`, `--sort name|count-asc|count-desc`, `--top N`, `--min-count N`, `--max-per-tag N`, `--color`
  - `tags compact`: one-line; supports `--group`, `--source`, `--sort name|count-asc|count-desc`, `--top N`, `--min-count N`, `--max-per-tag N`, `--color`
  - Shortcut: `rssel list --list-tags` also supports `--group`, `--tags-sort`, `--tags-top`
- `rssel view` — open in pager; `read` alias
- Short aliases: `l`=list, `ls`=list, `s`=sync, `p`=pick, `v`=view, `o`=open, `m`=mark, `read`=view
- `rssel open` — open link(s) in browser
- `rssel export` — export outside the project tree
- `rssel copy` — copy fields to clipboard
  - Parts: `--part url|title|summary|content` (default from config `copy_default_part`)
  - Plain: `--plain` converts HTML to plain text when copying summary/content
  - Separator: uses `copy_separator` in config when copying multiple items
- `rssel star` — star/unstar favorites
- `rssel archive id|source|group|date` — archive utilities
  - `archive source`: toggles fetch inclusion via archived flag; use `--url` or `--id`; add `--delete-items` to mark items deleted (starred protected)
  - `archive id` / `archive date`: legacy soft-delete; prefer `delete` below for clarity
- `rssel delete id|source` — soft delete utilities
  - `delete id <id> [--force|--undo]`
  - `delete source --url URL|--id ID [--force|--undo]`
- `rssel source rm --url URL|--id ID [--yes] [--vacuum]` — remove a source from the DB (cascades to items and feed_groups)
- `rssel purge` — permanently delete data and VACUUM
  - Deletes only what you request; starred items are protected for date‑based purges
  - `--deleted`: remove items marked as deleted
  - `--before D` or `--older-days N`: remove read, non‑starred items older than date/time or N days
  - Narrow scope: `--group Gs`, `--source URL|ID` or `--source-id ID`
  - Clean up: `--clean-tags` removes tags with no items
  - Reclaim space: `--vacuum`
  - Safety: `--dry-run` shows counts without deleting
  - Shortcut: `rssel purge-deleted [--group Gs] [--source URL|ID|--source-id ID] [--clean-tags] [--vacuum] [--dry-run]`

Simple Aliases (fast flows)
- Archive source by id/url: `rssel a --id ID [--undo] [--delete-items]`  (alias of `archive source`)
- Remove source by id/url: `rssel rm --id ID --yes [--vacuum]`  (alias of `source rm`)
- Soft-delete items: `rssel trash id <id> [--undo|--force]`, `rssel trash source --id ID [--undo|--force]` (alias of `delete`)
- Purge deleted: `rssel pd [--group Gs] [--source URL|ID|--source-id ID] [--clean-tags] [--vacuum] [--dry-run]` (alias of `purge --deleted`)
Step‑by‑Step: Archive, Delete, Purge

Find sources and IDs
- DB summary: `rssel list --sources --sort-id` (shows ID, items, last date, [archived])
- Config view (with DB info): `rssel sources --with-db`

Archive a source (stop fetching)
- Archive by ID: `rssel a --id 6`
- Undo (make active): `rssel a --id 6 --undo`
- Also mark existing items deleted (starred protected): `rssel a --id 6 --delete-items`

Soft‑delete (trash) items
- One item: `rssel trash id 123`
- By source: `rssel trash source --id 6`
- Include starred too: add `--force`
- Undo (restore): add `--undo`

Purge deleted items (remove permanently)
- Simple: `rssel pd --source-id 6 --clean-tags --vacuum`
  - Scope by group: `rssel pd --group news`
  - Global purge: `rssel pd --clean-tags --vacuum`
- Advanced: `rssel purge --deleted [--group Gs] [--source URL|ID|--source-id ID] [--clean-tags] [--vacuum] [--dry-run]`

Remove a source row (and its items)
- Preview (dry run): `rssel rm --id 6` (prints what will be removed)
- Confirm: `rssel rm --id 6 --yes --vacuum`
- Optional: remove from config as well by editing `./.rssel/sources.json`

Highlighting
- Edit `./.rssel/highlights.txt` and add words/phrases (UTF‑8, `#` for comments).
- List with `--highlight` to mark matches with `!`, or `--only-highlight` to filter.

Tagging
- Stopwords: edit `./.rssel/stopwords.txt` (one per line; `#` comments).
- Auto‑tagging runs on sync by default, or manually via `tags auto`.
- Tokenizer is Unicode‑aware; Swedish characters (å/ä/ö) are preserved in tags.

Tooling
- Create a source entry from a URL:
  - `python tooling/source_from_url.py <url> [-g group1,group2] [--compact]`
  - Prints `{ "title": ..., "url": ..., "groups": [...] }` for `sources.json`.

Notes
- Clipboard: auto‑detects `wl-copy`, `xclip`, `xsel`, `pbcopy` (or set `clipboard_cmd`).
- Editor: uses `$RSSEL_EDITOR`, `$EDITOR`, or `editor` in config (default `nvim`).
- Pager: uses `$RSSEL_PAGER`/`$PAGER`, falls back to `less`/`more`.
- File tree: `./.rssel/fs/<group>/<id>-<slug>.<ext>` with a small metadata header.
 - Filenames use ASCII slugs (safe for most filesystems). Content/tags preserve Unicode.
 
Cold Storage (tar.gz)
- Archive filtered items to a tar.gz (default) or tar with the same filters as `list`:
  - Basic: `rssel cold --group news`
  - Custom name: `rssel cold -o ./backup/news-archive` (writes `news-archive.gz`)
  - Plain tar: `rssel cold -o ./backup/news-archive.tar --no-gzip`
  - Format inside tar: `--format md|txt|json|html` (default: md)
- Filters supported: `--group`, `--tags`, `--source URL|ID`, `--limit`, `--unread-only|--read|--star`, `--new`, `--since|--until|--on` with `--date-field`, `--query`, `--highlight-only` (with `--highlight`)
- Every archive includes `MANIFEST.json` with metadata: `generated_at`, `count`, `format`, and per‑item entries (id, group, title, link, published_ts/date, tags, path).

Stats
- Show database statistics (optionally filtered by group/source/date):
  - Basic: `rssel stats`
  - By group: `rssel stats --group news`
  - By source: `rssel stats --source 6`
  - Date range: `rssel stats --since 2025-10-01 --until 2025-10-18 --date-field created`
  - Color: `rssel stats --color` (use `--nocolor` to force plain)
  - Top lists: `rssel stats --top 20`
- Output includes: feed totals (active/archived), item totals (alive/unread/starred/deleted), last item date, items in the `new_hours` window, per‑group counts, top tags, and top sources.
