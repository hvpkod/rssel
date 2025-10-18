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
  - Display: `display_*`, `display_snippet_len`
  - Internal export: `export_dir`, `export_format`
  - External export: `external_export_dir`, `external_export_format`
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
      { "title": "Simon Sinek", "url": "https://simonsinek.com/feed/", "groups": ["ideas","tech"] },
      { "title": "This Week in Rust", "url": "https://this-week-in-rust.org/rss.xml", "groups": ["tech"] }
    ]
  }
  ```

Key Commands
- `rssel init` — create config and example sources
- `rssel sources` — show sources by group
  - Add `--with-db` to include DB status: archived flag and current item counts
  - Add `--include-db-only` with `--with-db` to also list DB-only sources not present in the config (useful after purges or config edits)
- `rssel sync` — fetch + auto‑tag + export
  - `--group Gs` `--dest DIR` `--format md|txt|json|html` `--clean` `--no-auto-tags` `--max-tags K` `--include-domain`
- `rssel fetch [--group Gs]` — fetch only
- `rssel list` — filter and print items
  - Filters: `--group Gs` `--tags T1,T2` `--limit N` `--unread-only|--read|--star` `--query Q`
  - Dates: `--since D` `--until D` `--on D` `--date-field published|created`
  - Source: `--source URL|ID` to filter; `--sources` (or `--list-sources`) to list a summary with ids
  - Sort: `--sort-id` (id desc), `--sort-id-rev` (id asc), `--sort-name` (title A→Z), `--sort-group` (group A→Z, then newest), `--sort-count` (by tag count desc), `--sort-date-new` (newest), `--sort-date-old` (oldest)
  - Show (non‑grid): `--show-source|--show-url|--show-tags|--show-path|--show-date|--show-snippet`
    - Suppress tags even if enabled in config: `--no-show-tags`
  - Grid: `--grid` prints a base row (id, markers, groups, title). To include extra metadata rows in grid, use `--grid-meta date,path,url,tags,source,snippet` (comma or space list). Each selected meta prints as `id  label: value` on its own row.
  - JSON: `--json` outputs a JSON array. By default includes base fields (id, title, read, published_ts, primary_group, groups, feed_url, highlight). Optional fields (date, source, link, path, tags, snippet) are included only if you also pass the corresponding `--show-*` flags. JSON is plain (no ANSI colors).
  - Color: `--color` enables ANSI colors; use `--nocolor` to disable
  - Highlight: `--highlight` (marks matches with `!`), `--highlight-only` (filter to matches)
- `rssel pick` — fuzzy filter (fzf if available) with same filters as list
-- `rssel tags auto|list|items|map|compact` — tagging tools
  - `tags list`: sort with `--sort name|count-asc|count-desc` (default: count-desc), limit with `--top N`
  - `tags map`: `--sort name|count-asc|count-desc`, `--top N`, `--min-count N`, `--max-per-tag N`, `--color`
  - `tags compact`: one-line; supports `--sort name|count-asc|count-desc`, `--top N`, `--min-count N`, `--max-per-tag N`, `--color`
  - Shortcut: `rssel list --list-tags` also supports `--group`, `--tags-sort`, `--tags-top`
- `rssel view` — open in pager; `read` alias
- Short aliases: `v` for view, `o` for open
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
 
