# tui-player

A simple terminal music player built with [Textual](https://github.com/Textualize/textual) for the UI, [mpv](https://mpv.io/) (driven over its JSON IPC socket, using mpv's own playlist) for audio playback, [chafa](https://hpjansson.org/chafa/) to render cover art in the terminal, and SplayLite as the library's backing store.

Requires `mpv` and `chafa` to be installed and on `PATH` (e.g. `brew install mpv chafa`).

## SplayLite backend

The song library (path/title/artist/album) is stored in a `songs` table in SplayLite (`~/splaylite`), an in-memory SQL engine, via its C-API (loaded with `ctypes`). Adding a folder inserts new rows and reloads the library from the table, so SplayLite — not a plain Python list — is the source of truth for what's in the library and for path-based dedup.

Note: SplayLite's `WHERE` clause only supports equality on a table's key column (no filtering by arbitrary columns like `artist` or `album`), so search/grouping still happens in Python after fetching the full table — SplayLite here is genuinely the storage layer, not a query engine for search.

The engine (`libsplaylite.dylib`) is vendored in `lib/` so the player works standalone. Path resolution order: `SPLAYLITE_LIB` env var (exact path) → `lib/libsplaylite.dylib` next to this project → `~/splaylite/zig-out/lib/libsplaylite.dylib` (or `SPLAYLITE_REPO`) as a last resort, e.g. if you've rebuilt splaylite and want the freshest copy. If no library can be loaded, the player falls back to a plain in-memory list automatically — no crash, just no persistence-layer backing.

### Persistence across restarts

SplayLite itself is in-memory only and resets whenever the process exits, so on its own the library would disappear every time you quit. To fix that, the player also keeps a small cache at `~/.config/tui-player/library.json` (path/title/artist/album for every song) and, on startup, replays it into a fresh `songs` table automatically — songs whose file no longer exists on disk are dropped rather than restored. This cache is written every time you add a folder, and it's used whether or not SplayLite is available (the in-memory fallback restores from it too).

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
| `Enter`  | Play the highlighted song now (replaces the queue) / confirm search / jump to a queue item |
| `a`      | Add the highlighted song to the queue |
| `d`      | Remove the highlighted item from the queue (while the queue panel is focused) |
| `Esc`    | Cancel search                     |
| `Space`  | Pause / resume                   |
| `n`      | Jump to the next artist/album group (while grouped) |
| `←` / `→`| Seek -5s / +5s                    |
| `s`      | Stop (clears the queue)          |
| `+` / `-`| Volume up / down                 |
| `z`      | Toggle shuffle                    |
| `r`      | Cycle repeat: off → all → one     |
| `p`      | Save the current view as a named playlist |
| `P`      | Load a saved playlist (replaces the queue) |
| `q`      | Quit                              |

When adding a folder, the path field autocompletes as you type: `Tab`/`↓` to accept a suggestion, arrow keys to navigate, `Enter` to confirm. Directories containing audio (including nested) are sorted first. Songs already in the library (same file path) are skipped when adding overlapping folders.

Search matches fuzzily (subsequence, not just substring — `"frfl"` finds "Fireflies"), ranked by how tightly the match is clustered. Searching by artist or album groups results under a header per artist/album; searching by title stays a flat list.

The seek bar and volume bar live inside the song info panel and are clickable — click anywhere on either to jump to that position/level.

**Queue**: `Enter` on a library song plays it now, replacing whatever's queued; `a` appends the highlighted song to the queue instead (starting playback if nothing's currently playing). The queue panel on the right shows what's up, with the current track marked; `Enter`/click a queue item to jump straight to it, `d` to remove one.

Playback is driven by mpv's own playlist (`loadfile ... append`/`append-play`), not by this app reloading files one at a time — mpv itself advances between tracks, and this app's UI (seek bar, queue highlight, pause state) just follows along by observing mpv's playlist position and pause state, so it stays correct even when something else drives mpv directly.

On macOS, mpv registers for system media keys by default (`--input-media-keys`, on unless you rebuild with it off) and this app also launches it as a background/accessory app (no Dock icon) so it doesn't need focus to receive them. Whether your hardware/keyboard media keys actually reach *this* mpv instance rather than Music.app, Spotify, a browser, etc. depends on macOS's Now Playing arbitration among whatever's currently registered — it isn't something this app can force, only make mpv eligible for.

**Shuffle & repeat** are mpv-native: `z` calls mpv's `playlist-shuffle`/`playlist-unshuffle` (this app then re-reads mpv's actual order so the queue panel and shuffle stay in sync — including after a media-key-triggered change). `r` cycles `loop-playlist`/`loop-file`: `off` (stop at the end of the queue), `all` (loop the queue), `one` (replay the current track).

**Playlists**: `p` saves whatever's currently displayed (e.g. a search/filter result) as a named playlist; saving under an existing name overwrites it. `P` lists saved playlists to load back — loading one replaces the queue and starts playing it, in saved order. Playlists are stored the same way as the library (a SplayLite table, or a JSON file at `~/.config/tui-player/playlists.json` when SplayLite isn't available).

Album art is cached per song after the first render, and the player prefetches (renders in the background) whatever would play next, so advancing tracks doesn't have to wait on chafa.

Supported formats: mp3, wav, ogg, flac, m4a, opus, aac, wma (whatever your mpv build supports).

Cover art is pulled from embedded tags (ID3/FLAC/MP4) or a `cover`/`folder`/`front`/`album` image file next to the song, then rendered with chafa.

## License

[MIT](LICENSE)
