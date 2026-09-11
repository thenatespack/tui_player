# tui-player

A simple terminal music player built with [Textual](https://github.com/Textualize/textual) for the UI, [mpv](https://mpv.io/) (driven over its JSON IPC socket) for audio playback, [chafa](https://hpjansson.org/chafa/) to render cover art in the terminal, and SplayLite (`~/splaylite`) as the library's backing store.

Requires `mpv` and `chafa` to be installed and on `PATH` (e.g. `brew install mpv chafa`).

## SplayLite backend

The song library (path/title/artist/album) is stored in a `songs` table in SplayLite (`~/splaylite`), an in-memory SQL engine, via its C-API (`libsplaylite.dylib`, loaded with `ctypes`). Adding a folder inserts new rows and reloads the library from the table, so SplayLite — not a plain Python list — is the source of truth for what's in the library and for path-based dedup.

Note: SplayLite's `WHERE` clause only supports equality on a table's key column (no filtering by arbitrary columns like `artist` or `album`), so search/grouping still happens in Python after fetching the full table — SplayLite here is genuinely the storage layer, not a query engine for search.

By default the player looks for the library at `~/splaylite/zig-out/lib/libsplaylite.dylib` (built via `zig build` in that repo). Override with the `SPLAYLITE_LIB` env var (full path) or `SPLAYLITE_REPO` (repo root). If the library can't be loaded, the player falls back to a plain in-memory list automatically — no crash, just no persistence-layer backing.

## Usage

```sh
uv run main.py
```

## Keybindings

| Key      | Action                          |
| -------- | -------------------------------- |
| `o`      | Add a folder of music            |
| `/`      | Search the library                |
| `?`      | (while searching) cycle search field: title / artist / album |
| `Enter`  | Play the highlighted song / confirm search |
| `Esc`    | Cancel search                     |
| `Space`  | Pause / resume                   |
| `n`      | Jump to the next artist/album group (while grouped) |
| `s`      | Stop                             |
| `+` / `-`| Volume up / down                 |
| `q`      | Quit                              |

When adding a folder, the path field autocompletes as you type: `Tab`/`↓` to accept a suggestion, arrow keys to navigate, `Enter` to confirm. Directories containing audio (including nested) are sorted first. Songs already in the library (same file path) are skipped when adding overlapping folders.

Searching by artist or album groups results under a header per artist/album; searching by title stays a flat list.

Supported formats: mp3, wav, ogg, flac, m4a, opus, aac, wma (whatever your mpv build supports).

Cover art is pulled from embedded tags (ID3/FLAC/MP4) or a `cover`/`folder`/`front`/`album` image file next to the song, then rendered with chafa.
