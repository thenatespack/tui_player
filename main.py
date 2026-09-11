from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, OptionList, Static

from artwork import (
    find_cover_bytes,
    format_bit_depth,
    format_bitrate,
    format_duration,
    format_sample_rate,
    get_song_info,
    render_with_chafa,
)
from mpv_ipc import MPVError, MPVPlayer
from splaylite_client import SplayLite, SplayLiteError, sql_string

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus", ".aac", ".wma"}

ALBUM_ART_WIDTH = 40
ALBUM_ART_HEIGHT = 20

SEARCH_FIELDS = ("title", "artist", "album")

LIBRARY_CACHE_PATH = Path.home() / ".config" / "tui-player" / "library.json"
PLAYLIST_CACHE_PATH = Path.home() / ".config" / "tui-player" / "playlists.json"

SEEK_STEP_SECONDS = 5

REPEAT_MODES = ("off", "all", "one")


def render_bar(fraction: float, width: int) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "█" * filled + "─" * (width - filled)


@dataclass
class Song:
    path: Path
    title: str
    artist: str | None = None
    album: str | None = None


def _dir_has_audio(path: Path, max_depth: int = 2, scan_cap: int = 200) -> bool:
    """Cheaply check (bounded depth/breadth) whether a directory contains audio files."""
    try:
        with os.scandir(path) as it:
            subdirs = []
            for i, entry in enumerate(it):
                if i >= scan_cap:
                    break
                if entry.is_file() and Path(entry.name).suffix.lower() in AUDIO_EXTENSIONS:
                    return True
                if entry.is_dir() and not entry.name.startswith("."):
                    subdirs.append(entry.path)
    except OSError:
        return False

    if max_depth > 0:
        for sub in subdirs:
            if _dir_has_audio(Path(sub), max_depth - 1, scan_cap):
                return True
    return False


def get_path_suggestions(text: str, limit: int = 12) -> list[Path]:
    """Directories matching the folder path currently being typed."""
    if text == "":
        base_dir = Path.home()
        prefix = ""
    else:
        expanded = os.path.expanduser(text)
        if text.endswith(os.sep):
            base_dir = Path(expanded)
            prefix = ""
        else:
            candidate = Path(expanded)
            base_dir = candidate.parent if str(candidate.parent) else Path(".")
            prefix = candidate.name

    if not base_dir.is_dir():
        return []

    try:
        entries = [e for e in base_dir.iterdir() if e.is_dir()]
    except OSError:
        return []

    show_hidden = prefix.startswith(".")
    prefix_lower = prefix.lower()
    filtered = [
        e for e in entries
        if (show_hidden or not e.name.startswith(".")) and e.name.lower().startswith(prefix_lower)
    ]
    matches = sorted(filtered, key=lambda e: (not _dir_has_audio(e), e.name.lower()))
    return matches[:limit]


class SearchInput(Input):
    """An Input that lets '?' cycle the search field instead of being typed."""

    async def _on_key(self, event: events.Key) -> None:
        if event.character == "?":
            event.stop()
            event.prevent_default()
            self.app.action_cycle_search_field()
            return
        await super()._on_key(event)


def fuzzy_score(query: str, text: str) -> float | None:
    """None if `query`'s characters don't all appear in `text` in order; else a compactness
    score where lower is a better match (tightly clustered matches beat scattered ones)."""
    if not query:
        return 0.0
    first: int | None = None
    last = 0
    qi = 0
    for i, ch in enumerate(text):
        if qi < len(query) and ch == query[qi]:
            if first is None:
                first = i
            last = i
            qi += 1
    if qi < len(query) or first is None:
        return None
    return float(last - first)


def song_label(song: Song) -> str:
    parts = [f"[b]{song.title}[/b]"]
    detail = " · ".join(p for p in (song.artist, song.album) if p)
    if detail:
        parts.append(f"[dim]{detail}[/dim]")
    return "  ".join(parts)


def find_songs(folder: Path) -> list[Song]:
    songs = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            if Path(name).suffix.lower() in AUDIO_EXTENSIONS:
                path = Path(root) / name
                info = get_song_info(path)
                songs.append(Song(path=path, title=info.title, artist=info.artist, album=info.album))
    songs.sort(key=lambda s: s.title.lower())
    return songs


class AddFolderScreen(ModalScreen[str]):
    """Modal dialog to type a folder path to add, with live directory autocomplete."""

    DEFAULT_CSS = """
    AddFolderScreen {
        align: center middle;
    }
    #dialog {
        width: 70%;
        height: auto;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #dialog Label {
        margin-bottom: 1;
    }
    #suggestions {
        height: auto;
        max-height: 10;
        margin-top: 1;
        border: round $primary;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._suggestions: list[Path] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Type a folder path (Tab/↓ to autocomplete, Enter to confirm, Esc to cancel):")
            yield Input(placeholder="/path/to/music", id="folder_input")
            yield OptionList(id="suggestions")

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self._refresh_suggestions("")

    def _refresh_suggestions(self, text: str) -> None:
        self._suggestions = get_path_suggestions(text)
        option_list = self.query_one(OptionList)
        option_list.clear_options()
        for path in self._suggestions:
            option_list.add_option(str(path))
        option_list.display = bool(self._suggestions)

    def _accept_suggestion(self, path: Path) -> None:
        new_value = str(path) + os.sep
        input_widget = self.query_one(Input)
        input_widget.value = new_value
        input_widget.cursor_position = len(new_value)
        input_widget.focus()
        self._refresh_suggestions(new_value)

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        self._refresh_suggestions(event.value)

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    @on(OptionList.OptionSelected)
    def suggestion_selected(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self._suggestions):
            self._accept_suggestion(self._suggestions[event.option_index])

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss("")
        elif event.key == "down" and self.focused is self.query_one(Input) and self._suggestions:
            event.stop()
            event.prevent_default()
            option_list = self.query_one(OptionList)
            option_list.focus()
            option_list.highlighted = 0
        elif event.key == "tab" and self._suggestions:
            event.stop()
            event.prevent_default()
            self._accept_suggestion(self._suggestions[0])


class NamePlaylistScreen(ModalScreen[str]):
    """Modal dialog to type a name for a new (or existing, to overwrite) playlist."""

    DEFAULT_CSS = """
    NamePlaylistScreen {
        align: center middle;
    }
    #dialog {
        width: 60%;
        height: auto;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #dialog Label {
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Save the current view as a playlist (Enter to confirm, Esc to cancel):")
            yield Input(placeholder="Playlist name", id="name_input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss("")


class LoadPlaylistScreen(ModalScreen[str]):
    """Modal dialog listing saved playlists to load."""

    DEFAULT_CSS = """
    LoadPlaylistScreen {
        align: center middle;
    }
    #dialog {
        width: 60%;
        height: auto;
        border: round $accent;
        background: $panel;
        padding: 1 2;
    }
    #dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self._names = names

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Load a playlist (Enter to select, Esc to cancel):")
            option_list = OptionList(*self._names, id="playlist_list")
            yield option_list

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def selected(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self._names):
            self.dismiss(self._names[event.option_index])

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss("")


class AlbumArt(Static):
    """Renders a song's cover art as terminal art via chafa."""

    DEFAULT_CSS = """
    AlbumArt {
        width: auto;
        height: auto;
        border: round $primary;
        padding: 1;
        content-align: center middle;
    }
    """

    def set_ansi(self, ansi: str | None) -> None:
        if ansi is None:
            self.update(Text("No cover art", style="dim"))
        else:
            self.update(Text.from_ansi(ansi))

    def clear(self) -> None:
        self.update(Text("Nothing playing", style="dim"))


TIME_LABEL_WIDTH = 5  # "MM:SS"


class SeekBar(Static, can_focus=True):
    """Shows playback position as a bar between elapsed and total time. Click to seek."""

    BAR_WIDTH = 24
    PREFIX_WIDTH = TIME_LABEL_WIDTH + 2  # time label + " ["

    DEFAULT_CSS = """
    SeekBar {
        width: auto;
        height: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._duration: float | None = None

    def show(self, position: float | None, duration: float | None) -> None:
        self._duration = duration
        pos_label = format_duration(position).rjust(TIME_LABEL_WIDTH)
        dur_label = format_duration(duration).ljust(TIME_LABEL_WIDTH)
        fraction = (position or 0.0) / duration if duration else 0.0
        bar = render_bar(fraction, self.BAR_WIDTH)
        self.update(f"{pos_label} [{bar}] {dur_label}")

    def clear(self) -> None:
        self._duration = None
        self.show(None, None)

    async def on_click(self, event: events.Click) -> None:
        app: MusicPlayerApp = self.app  # type: ignore[assignment]
        if not self._duration or app.current_song() is None:
            return
        fraction = (event.x - self.PREFIX_WIDTH) / self.BAR_WIDTH
        fraction = max(0.0, min(1.0, fraction))
        await app.mpv.seek_absolute(fraction * self._duration)
        await app._update_seek_bar()


class VolumeBar(Static, can_focus=True):
    """Shows the current output volume as a bar. Click to set the volume."""

    BAR_WIDTH = 12
    PREFIX_WIDTH = 5  # "Vol ["

    DEFAULT_CSS = """
    VolumeBar {
        width: auto;
        height: 1;
    }
    """

    def show(self, volume: float) -> None:
        bar = render_bar(volume, self.BAR_WIDTH)
        self.update(f"Vol [{bar}] {round(volume * 100)}%")

    async def on_click(self, event: events.Click) -> None:
        app: MusicPlayerApp = self.app  # type: ignore[assignment]
        fraction = (event.x - self.PREFIX_WIDTH) / self.BAR_WIDTH
        fraction = max(0.0, min(1.0, fraction))
        app.volume = fraction
        await app.mpv.set_volume(fraction)
        self.show(fraction)
        app.set_status(f"Volume: {round(fraction * 100)}%")


class PlaybackModesBar(Static):
    """Shows the current shuffle/repeat state."""

    DEFAULT_CSS = """
    PlaybackModesBar {
        width: auto;
        height: 1;
        color: $text-muted;
    }
    """

    def show(self, shuffle: bool, repeat: str) -> None:
        self.update(f"Shuffle: {'on' if shuffle else 'off'}   Repeat: {repeat}")


class SongInfoPanel(Vertical):
    """Shows title/artist/album/duration/tech info for the current song, plus seek/volume bars."""

    DEFAULT_CSS = """
    SongInfoPanel {
        width: auto;
        min-width: 44;
        height: auto;
        border: round $primary;
        padding: 0 1;
    }
    SongInfoPanel #info_text {
        width: auto;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="info_text")
        yield SeekBar(id="seek_bar")
        yield VolumeBar(id="volume_bar")
        yield PlaybackModesBar(id="playback_modes")

    def show_song(self, song: Song) -> None:
        info = get_song_info(song.path)
        lines = [f"[b]{song.title}[/b]"]
        if song.artist:
            lines.append(song.artist)
        if song.album:
            lines.append(song.album)
        lines.append(format_duration(info.duration))

        tech = [info.file_type, format_bitrate(info.bitrate), format_sample_rate(info.sample_rate)]
        if info.bit_depth:
            tech.append(format_bit_depth(info.bit_depth))
        if info.channels:
            tech.append(f"{info.channels}ch")
        lines.append("[dim]" + " · ".join(tech) + "[/dim]")

        self.query_one("#info_text", Static).update("\n".join(lines))
        self.query_one(SeekBar).show(0, info.duration)

    def clear(self) -> None:
        self.query_one("#info_text", Static).update(Text("", style="dim"))
        self.query_one(SeekBar).clear()


class MusicPlayerApp(App):
    TITLE = "TUI Music Player"

    CSS = """
    #status {
        height: 3;
        border: round $accent;
        padding: 0 1;
        content-align: left middle;
    }
    #main {
        height: 1fr;
    }
    #library_pane {
        width: 1fr;
    }
    #search_input {
        display: none;
        border: round $accent;
    }
    #library {
        height: 1fr;
        border: round $primary;
    }
    #library ListItem.group-header {
        background: $panel;
    }
    #queue_pane {
        width: 32;
        height: 1fr;
    }
    #queue_label {
        height: 1;
        color: $text-muted;
    }
    #queue_list {
        height: 1fr;
        border: round $primary;
    }
    #queue_list ListItem.now-playing {
        background: $panel;
        color: $accent;
    }
    """

    BINDINGS = [
        Binding("o", "add_folder", "Add folder"),
        Binding("slash", "search", "Search"),
        Binding("question_mark", "cycle_search_field", "Search field", show=False),
        Binding("escape", "cancel_search", "Cancel search", show=False),
        Binding("enter", "play_selected", "Play"),
        Binding("a", "add_to_queue", "Add to queue"),
        Binding("d", "remove_from_queue", "Remove from queue", show=False),
        Binding("space", "toggle_pause", "Pause/Resume"),
        Binding("n", "next_group", "Next artist/album"),
        Binding("s", "stop", "Stop"),
        Binding("left", "seek_back", "Seek -5s"),
        Binding("right", "seek_forward", "Seek +5s"),
        Binding("minus", "volume_down", "Vol -"),
        Binding("equals", "volume_up", "Vol +"),
        Binding("z", "toggle_shuffle", "Shuffle"),
        Binding("r", "cycle_repeat", "Repeat"),
        Binding("p", "save_playlist", "Save playlist"),
        Binding("P", "load_playlist", "Load playlist"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.songs: list[Song] = []
        self.displayed: list[Song] = []
        self.queue: list[Song] = []
        self.queue_pos: int = -1
        self.paused = False
        self.volume = 0.7
        self.search_field = SEARCH_FIELDS[0]
        self._pre_search_index: int | None = None
        self._known_paths: set[Path] = set()
        self._row_songs: list[Song | None] = []
        self._group_starts: list[int] = []
        self.shuffle = False
        self.repeat = REPEAT_MODES[0]
        self._shutting_down = False
        self._art_cache: dict[Path, str | None] = {}
        self.mpv = MPVPlayer(
            on_playlist_pos_change=self._on_playlist_pos_change,
            on_pause_change=self._on_pause_change,
        )
        try:
            self.db: SplayLite | None = SplayLite()
        except OSError:
            self.db = None

    def current_song(self) -> Song | None:
        if 0 <= self.queue_pos < len(self.queue):
            return self.queue[self.queue_pos]
        return None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Starting mpv...", id="status")
        with Horizontal(id="main"):
            with Vertical(id="library_pane"):
                yield SearchInput(placeholder="Search songs...", id="search_input")
                yield ListView(id="library")
            with Vertical(id="now_playing"):
                yield AlbumArt(id="album_art")
                yield SongInfoPanel(id="song_info")
            with Vertical(id="queue_pane"):
                yield Label("Queue", id="queue_label")
                yield ListView(id="queue_list")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one(AlbumArt).clear()
        self.query_one(SongInfoPanel).clear()
        self.query_one(VolumeBar).show(self.volume)
        self.query_one(PlaybackModesBar).show(self.shuffle, self.repeat)
        self.query_one("#library", ListView).focus()
        self.set_interval(0.5, self._update_seek_bar)

        db_error = False
        if self.db is not None:
            try:
                self.db.query(
                    "CREATE TABLE IF NOT EXISTS songs "
                    "(path TEXT NOT NULL, title TEXT NOT NULL, artist TEXT, album TEXT);"
                )
                self.db.query(
                    "CREATE TABLE IF NOT EXISTS playlist_songs "
                    "(playlist TEXT NOT NULL, path TEXT NOT NULL, position INTEGER NOT NULL);"
                )
            except SplayLiteError as e:
                self.set_status(f"splaylite unavailable, using in-memory library ({e})")
                self.db = None
                db_error = True

        restored = self._restore_library()

        try:
            await self.mpv.start()
            await self.mpv.set_volume(self.volume)
            if not restored and not db_error:
                self.set_status("Press 'o' to add a music folder.")
        except MPVError as e:
            self.set_status(f"mpv error: {e}")

    async def on_unmount(self) -> None:
        self._shutting_down = True
        await self.mpv.shutdown()
        if self.db is not None:
            self.db.deinit()

    async def action_quit(self) -> None:
        self._shutting_down = True
        await self.mpv.shutdown()
        if self.db is not None:
            self.db.deinit()
        self.exit()

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def action_add_folder(self) -> None:
        def handle_result(path_str: str) -> None:
            if not path_str:
                return
            folder = Path(path_str).expanduser()
            if not folder.is_dir():
                self.set_status(f"Not a folder: {folder}")
                return
            found = find_songs(folder)
            if not found:
                self.set_status(f"No audio files found in {folder}")
                return
            new_songs = [s for s in found if s.path not in self._known_paths]
            duplicates = len(found) - len(new_songs)
            if not new_songs:
                self.set_status(f"All {duplicates} song(s) in {folder} are already in the library")
                return

            had_songs = bool(self.songs)
            if self.db is not None:
                for song in new_songs:
                    self.db.query(
                        "INSERT INTO songs (path, title, artist, album) VALUES "
                        f"({sql_string(str(song.path))}, {sql_string(song.title)}, "
                        f"{sql_string(song.artist)}, {sql_string(song.album)});"
                    )
                self._reload_from_db()
            else:
                self._known_paths.update(s.path for s in new_songs)
                self.songs.extend(new_songs)
                self.songs.sort(key=lambda s: s.title.lower())
                self.displayed = list(self.songs)
            search_input = self.query_one("#search_input", Input)
            if search_input.display:
                search_input.display = False
            keep_index = self.query_one("#library", ListView).index if had_songs else None
            self._render_list(self.displayed, keep_index)
            self._write_library_cache()

            msg = f"Added {len(new_songs)} song(s) from {folder}"
            if duplicates:
                msg += f" ({duplicates} duplicate(s) skipped)"
            self.set_status(msg)

        self.push_screen(AddFolderScreen(), handle_result)

    def _read_library_cache(self) -> list[dict]:
        try:
            return json.loads(LIBRARY_CACHE_PATH.read_text())
        except (OSError, ValueError):
            return []

    def _write_library_cache(self) -> None:
        try:
            LIBRARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = [
                {"path": str(s.path), "title": s.title, "artist": s.artist, "album": s.album}
                for s in self.songs
            ]
            LIBRARY_CACHE_PATH.write_text(json.dumps(data))
        except OSError:
            pass

    def _restore_library(self) -> bool:
        """Reload the library saved by a previous session. Returns True if anything was restored."""
        cached = self._read_library_cache()
        valid = [c for c in cached if Path(c["path"]).is_file()]
        if not valid:
            return False

        if self.db is not None:
            for entry in valid:
                self.db.query(
                    "INSERT INTO songs (path, title, artist, album) VALUES "
                    f"({sql_string(entry['path'])}, {sql_string(entry['title'])}, "
                    f"{sql_string(entry.get('artist'))}, {sql_string(entry.get('album'))});"
                )
            self._reload_from_db()
        else:
            self.songs = [
                Song(path=Path(e["path"]), title=e["title"], artist=e.get("artist"), album=e.get("album"))
                for e in valid
            ]
            self.songs.sort(key=lambda s: s.title.lower())
            self.displayed = list(self.songs)
            self._known_paths = {s.path for s in self.songs}

        self._render_list(self.displayed, None)
        skipped = len(cached) - len(valid)
        msg = f"Restored {len(valid)} song(s) from last session"
        if skipped:
            msg += f" ({skipped} missing file(s) skipped)"
        self.set_status(msg)
        return True

    def _reload_from_db(self) -> None:
        """Refresh self.songs from the splaylite `songs` table (source of truth)."""
        assert self.db is not None
        result = self.db.query("SELECT * FROM songs;")
        col_index = {c["name"]: i for i, c in enumerate(result["columns"])}
        songs = [
            Song(
                path=Path(row[col_index["path"]]),
                title=row[col_index["title"]],
                artist=row[col_index["artist"]],
                album=row[col_index["album"]],
            )
            for row in result["rows"]
        ]
        songs.sort(key=lambda s: s.title.lower())
        self.songs = songs
        self.displayed = list(songs)
        self._known_paths = {s.path for s in songs}

    def _read_playlist_rows(self) -> list[dict]:
        """All (playlist, path, position) rows, from splaylite or the JSON fallback."""
        if self.db is not None:
            result = self.db.query("SELECT * FROM playlist_songs;")
            col_index = {c["name"]: i for i, c in enumerate(result["columns"])}
            return [
                {
                    "playlist": row[col_index["playlist"]],
                    "path": row[col_index["path"]],
                    "position": row[col_index["position"]],
                }
                for row in result["rows"]
            ]
        try:
            return json.loads(PLAYLIST_CACHE_PATH.read_text())
        except (OSError, ValueError):
            return []

    def _write_playlist_rows_json(self, rows: list[dict]) -> None:
        try:
            PLAYLIST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            PLAYLIST_CACHE_PATH.write_text(json.dumps(rows))
        except OSError:
            pass

    def _save_playlist(self, name: str) -> None:
        if self.db is not None:
            result = self.db.query("SELECT * FROM playlist_songs;")
            col_index = {c["name"]: i for i, c in enumerate(result["columns"])}
            for row in result["rows"]:
                if row[col_index["playlist"]] == name:
                    self.db.query(f"DELETE FROM playlist_songs WHERE rowid = {row[col_index['rowid']]};")
            for i, song in enumerate(self.displayed):
                self.db.query(
                    "INSERT INTO playlist_songs (playlist, path, position) VALUES "
                    f"({sql_string(name)}, {sql_string(str(song.path))}, {i});"
                )
        else:
            rows = [r for r in self._read_playlist_rows() if r["playlist"] != name]
            rows.extend(
                {"playlist": name, "path": str(song.path), "position": i}
                for i, song in enumerate(self.displayed)
            )
            self._write_playlist_rows_json(rows)

    def _load_playlist(self, name: str) -> list[Song]:
        rows = sorted(
            (r for r in self._read_playlist_rows() if r["playlist"] == name),
            key=lambda r: r["position"],
        )
        by_path = {s.path: s for s in self.songs}
        return [by_path[Path(r["path"])] for r in rows if Path(r["path"]) in by_path]

    def action_save_playlist(self) -> None:
        if not self.displayed:
            self.set_status("Nothing to save — the current view is empty.")
            return

        def handle_result(name: str) -> None:
            if not name:
                return
            self._save_playlist(name)
            self.set_status(f"Saved playlist '{name}' ({len(self.displayed)} song(s))")

        self.push_screen(NamePlaylistScreen(), handle_result)

    def action_load_playlist(self) -> None:
        names = sorted({r["playlist"] for r in self._read_playlist_rows()})
        if not names:
            self.set_status("No saved playlists yet — press 'p' to save one.")
            return

        def handle_result(name: str) -> None:
            if not name:
                return
            songs = self._load_playlist(name)
            if not songs:
                self.set_status(f"Playlist '{name}' has no songs still in the library.")
                return
            self.displayed = songs
            self._render_list(self.displayed, None)
            self.run_worker(self._play_queue(songs), exclusive=True)
            self.set_status(f"Loaded playlist '{name}' ({len(songs)} song(s))")

        self.push_screen(LoadPlaylistScreen(names), handle_result)

    def _default_index(self) -> int | None:
        for i, song in enumerate(self._row_songs):
            if song is not None:
                return i
        return None

    def _song_at(self, index: int | None) -> Song | None:
        if index is None or not (0 <= index < len(self._row_songs)):
            return None
        return self._row_songs[index]

    def _render_list(self, songs: list[Song], index: int | None, group_field: str | None = None) -> None:
        list_view = self.query_one("#library", ListView)
        list_view.clear()
        self._row_songs = []
        self._group_starts = []

        if group_field:
            def group_key(song: Song) -> str:
                return getattr(song, group_field) or f"Unknown {group_field}"

            ordered = sorted(songs, key=lambda s: (group_key(s).lower(), s.title.lower()))
            current_group: object = object()
            for song in ordered:
                key = group_key(song)
                if key != current_group:
                    current_group = key
                    self._group_starts.append(len(self._row_songs))
                    list_view.append(ListItem(Label(f"[b]{key}[/b]"), disabled=True, classes="group-header"))
                    self._row_songs.append(None)
                list_view.append(ListItem(Label(song_label(song))))
                self._row_songs.append(song)
        else:
            for song in songs:
                list_view.append(ListItem(Label(song_label(song))))
                self._row_songs.append(song)

        list_view.index = index if index is not None else self._default_index()

    def _search_placeholder(self) -> str:
        return f"Search by {self.search_field} (? to change)..."

    def action_search(self) -> None:
        if not self.songs:
            return
        list_view = self.query_one("#library", ListView)
        self._pre_search_index = list_view.index
        search_input = self.query_one("#search_input", Input)
        search_input.placeholder = self._search_placeholder()
        search_input.value = ""
        search_input.display = True
        search_input.focus()

    def action_cancel_search(self) -> None:
        search_input = self.query_one("#search_input", Input)
        if not search_input.display:
            return
        search_input.display = False
        self.displayed = list(self.songs)
        self._render_list(self.displayed, self._pre_search_index)
        self.query_one("#library", ListView).focus()

    def action_next_group(self) -> None:
        if not self._group_starts:
            self.set_status("Not grouped — search by artist or album (press '/' then '?') to group.")
            return
        list_view = self.query_one("#library", ListView)
        current = list_view.index if list_view.index is not None else -1
        later = [i for i in self._group_starts if i > current]
        target_header = later[0] if later else self._group_starts[0]
        list_view.index = target_header + 1

    def action_cycle_search_field(self) -> None:
        current = SEARCH_FIELDS.index(self.search_field)
        self.search_field = SEARCH_FIELDS[(current + 1) % len(SEARCH_FIELDS)]
        search_input = self.query_one("#search_input", Input)
        search_input.placeholder = self._search_placeholder()
        if search_input.display:
            self._apply_search(search_input.value)
        self.set_status(f"Searching by {self.search_field}")

    def _apply_search(self, value: str) -> None:
        query = value.strip().lower()
        if query:
            scored = (
                (fuzzy_score(query, (getattr(s, self.search_field) or "").lower()), s)
                for s in self.songs
            )
            ranked = [(score, s) for score, s in scored if score is not None]
            ranked.sort(key=lambda pair: pair[0])
            self.displayed = [s for _, s in ranked]
        else:
            self.displayed = list(self.songs)
        group_field = self.search_field if self.search_field != "title" else None
        self._render_list(self.displayed, None, group_field)

    @on(Input.Changed, "#search_input")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._apply_search(event.value)

    @on(Input.Submitted, "#search_input")
    def on_search_submitted(self, event: Input.Submitted) -> None:
        self.query_one("#search_input", Input).display = False
        self.query_one("#library", ListView).focus()

    async def action_play_selected(self) -> None:
        list_view = self.query_one("#library", ListView)
        if self.focused is not list_view:
            return
        index = list_view.index if list_view.index is not None else self._default_index()
        song = self._song_at(index)
        if song is not None:
            await self.play_song(song)

    async def play_song(self, song: Song) -> None:
        """Play now, replacing whatever's queued."""
        await self._play_queue([song])
        list_view = self.query_one("#library", ListView)
        list_view.index = self._row_songs.index(song) if song in self._row_songs else None

    async def _play_queue(self, songs: list[Song]) -> None:
        if not songs:
            return
        self.queue = list(songs)
        try:
            await self.mpv.stop()
            await self.mpv.playlist_clear()
            for i, song in enumerate(songs):
                await self.mpv.playlist_append(str(song.path), play=(i == 0))
        except MPVError as e:
            self.set_status(f"Error playing {songs[0].title}: {e}")
            return
        self._render_queue_panel()

    async def action_add_to_queue(self) -> None:
        list_view = self.query_one("#library", ListView)
        if self.focused is not list_view:
            return
        song = self._song_at(list_view.index if list_view.index is not None else self._default_index())
        if song is None:
            return
        was_idle = self.current_song() is None
        try:
            await self.mpv.playlist_append(str(song.path), play=was_idle)
        except MPVError as e:
            self.set_status(f"Error queueing {song.title}: {e}")
            return
        self.queue.append(song)
        self._render_queue_panel()
        self.set_status(f"Queued: {song.title}")

    async def action_remove_from_queue(self) -> None:
        queue_list = self.query_one("#queue_list", ListView)
        if self.focused is not queue_list or queue_list.index is None:
            return
        index = queue_list.index
        if not (0 <= index < len(self.queue)):
            return
        removed = self.queue[index].title
        await self.mpv.playlist_remove(index)
        await self._sync_queue_from_mpv()
        self.set_status(f"Removed from queue: {removed}")

    def _render_queue_panel(self) -> None:
        queue_list = self.query_one("#queue_list", ListView)
        queue_list.clear()
        for i, song in enumerate(self.queue):
            marker = "▶ " if i == self.queue_pos else "  "
            item = ListItem(Label(marker + song_label(song)))
            if i == self.queue_pos:
                item.add_class("now-playing")
            queue_list.append(item)

    async def _sync_queue_from_mpv(self) -> None:
        """Rebuild self.queue/self.queue_pos from mpv's actual playlist (source of truth
        after shuffle/unshuffle/remove, which mpv performs internally)."""
        entries = await self.mpv.get_playlist()
        by_path = {str(s.path): s for s in self.songs}
        self.queue = [by_path[e["filename"]] for e in entries if e.get("filename") in by_path]
        self.queue_pos = await self.mpv.get_playlist_pos()
        self._render_queue_panel()

    async def get_art_ansi(self, song: Song) -> str | None:
        if song.path not in self._art_cache:
            cover = find_cover_bytes(song.path)
            ansi = await render_with_chafa(cover, ALBUM_ART_WIDTH, ALBUM_ART_HEIGHT) if cover else None
            self._art_cache[song.path] = ansi
        return self._art_cache[song.path]

    async def _show_art_and_prefetch(self, song: Song) -> None:
        ansi = await self.get_art_ansi(song)
        self.query_one(AlbumArt).set_ansi(ansi)
        next_pos = self.queue_pos + 1
        if 0 <= next_pos < len(self.queue):
            await self.get_art_ansi(self.queue[next_pos])

    async def _on_playlist_pos_change(self, pos: int) -> None:
        if self._shutting_down:
            return
        self.queue_pos = pos
        self._render_queue_panel()
        song = self.current_song()
        if song is None:
            self.set_status("Queue finished." if self.queue else "Stopped.")
            self.query_one(AlbumArt).clear()
            self.query_one(SongInfoPanel).clear()
            return
        self.set_status(f"Playing: {song.title}")
        list_view = self.query_one("#library", ListView)
        list_view.index = self._row_songs.index(song) if song in self._row_songs else None
        self.query_one(SongInfoPanel).show_song(song)
        self.run_worker(self._show_art_and_prefetch(song), exclusive=True, group="art")

    async def _on_pause_change(self, paused: bool) -> None:
        if self._shutting_down:
            return
        self.paused = paused

    async def _update_seek_bar(self) -> None:
        if self.current_song() is None or self._shutting_down:
            return
        position = await self.mpv.get_time_pos()
        duration = await self.mpv.get_duration()
        self.query_one(SeekBar).show(position, duration)

    async def action_seek_back(self) -> None:
        if self.current_song() is None:
            return
        await self.mpv.seek_relative(-SEEK_STEP_SECONDS)
        await self._update_seek_bar()

    async def action_seek_forward(self) -> None:
        if self.current_song() is None:
            return
        await self.mpv.seek_relative(SEEK_STEP_SECONDS)
        await self._update_seek_bar()

    async def action_toggle_shuffle(self) -> None:
        self.shuffle = not self.shuffle
        if self.shuffle:
            await self.mpv.playlist_shuffle()
        else:
            await self.mpv.playlist_unshuffle()
        await self._sync_queue_from_mpv()
        self.query_one(PlaybackModesBar).show(self.shuffle, self.repeat)
        self.set_status(f"Shuffle: {'on' if self.shuffle else 'off'}")

    async def action_cycle_repeat(self) -> None:
        current = REPEAT_MODES.index(self.repeat)
        self.repeat = REPEAT_MODES[(current + 1) % len(REPEAT_MODES)]
        await self.mpv.set_loop_playlist("inf" if self.repeat == "all" else "no")
        await self.mpv.set_loop_file("inf" if self.repeat == "one" else "no")
        self.query_one(PlaybackModesBar).show(self.shuffle, self.repeat)
        self.set_status(f"Repeat: {self.repeat}")

    async def action_toggle_pause(self) -> None:
        if self.current_song() is None:
            return
        await self.mpv.set_pause(not self.paused)

    async def action_stop(self) -> None:
        await self.mpv.stop()
        await self.mpv.playlist_clear()
        self.queue = []
        self.queue_pos = -1
        self.paused = False
        self.set_status("Stopped.")
        self.query_one(AlbumArt).clear()
        self.query_one(SongInfoPanel).clear()
        self._render_queue_panel()

    async def action_volume_up(self) -> None:
        self.volume = min(1.0, self.volume + 0.1)
        await self.mpv.set_volume(self.volume)
        self.query_one(VolumeBar).show(self.volume)
        self.set_status(f"Volume: {round(self.volume * 100)}%")

    async def action_volume_down(self) -> None:
        self.volume = max(0.0, self.volume - 0.1)
        await self.mpv.set_volume(self.volume)
        self.query_one(VolumeBar).show(self.volume)
        self.set_status(f"Volume: {round(self.volume * 100)}%")

    @on(ListView.Selected, "#library")
    async def on_library_selected(self, event: ListView.Selected) -> None:
        song = self._song_at(self.query_one("#library", ListView).index)
        if song is not None:
            await self.play_song(song)

    @on(ListView.Selected, "#queue_list")
    async def on_queue_selected(self, event: ListView.Selected) -> None:
        queue_list = self.query_one("#queue_list", ListView)
        index = queue_list.index
        if index is None or not (0 <= index < len(self.queue)):
            return
        await self.mpv.playlist_play_index(index)


def main() -> None:
    MusicPlayerApp().run()


if __name__ == "__main__":
    main()
