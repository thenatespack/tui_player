from __future__ import annotations

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

    async def show_song(self, song_path: Path) -> None:
        cover = find_cover_bytes(song_path)
        if not cover:
            self.update(Text("No cover art", style="dim"))
            return
        ansi = await render_with_chafa(cover, ALBUM_ART_WIDTH, ALBUM_ART_HEIGHT)
        if not ansi:
            self.update(Text("chafa not available", style="dim"))
            return
        self.update(Text.from_ansi(ansi))

    def clear(self) -> None:
        self.update(Text("Nothing playing", style="dim"))


class SongInfoPanel(Static):
    """Shows title/artist/album/duration for the current song."""

    DEFAULT_CSS = """
    SongInfoPanel {
        width: auto;
        min-width: 44;
        height: auto;
        border: round $primary;
        padding: 0 1;
    }
    """

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

        self.update("\n".join(lines))

    def clear(self) -> None:
        self.update(Text("", style="dim"))


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
    """

    BINDINGS = [
        Binding("o", "add_folder", "Add folder"),
        Binding("slash", "search", "Search"),
        Binding("question_mark", "cycle_search_field", "Search field", show=False),
        Binding("escape", "cancel_search", "Cancel search", show=False),
        Binding("enter", "play_selected", "Play"),
        Binding("space", "toggle_pause", "Pause/Resume"),
        Binding("n", "next_group", "Next artist/album"),
        Binding("s", "stop", "Stop"),
        Binding("minus", "volume_down", "Vol -"),
        Binding("equals", "volume_up", "Vol +"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.songs: list[Song] = []
        self.displayed: list[Song] = []
        self.current_index: int | None = None
        self.paused = False
        self.volume = 0.7
        self.search_field = SEARCH_FIELDS[0]
        self._pre_search_index: int | None = None
        self._known_paths: set[Path] = set()
        self._row_songs: list[Song | None] = []
        self._group_starts: list[int] = []
        self.mpv = MPVPlayer(on_eof=self._handle_eof)
        try:
            self.db: SplayLite | None = SplayLite()
        except OSError:
            self.db = None

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
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one(AlbumArt).clear()
        self.query_one(SongInfoPanel).clear()
        self.query_one("#library", ListView).focus()

        if self.db is not None:
            try:
                self.db.query(
                    "CREATE TABLE IF NOT EXISTS songs "
                    "(path TEXT NOT NULL, title TEXT NOT NULL, artist TEXT, album TEXT);"
                )
            except SplayLiteError as e:
                self.set_status(f"splaylite unavailable, using in-memory library ({e})")
                self.db = None

        try:
            await self.mpv.start()
            await self.mpv.set_volume(self.volume)
            if self.db is not None:
                self.set_status("Press 'o' to add a music folder.")
        except MPVError as e:
            self.set_status(f"mpv error: {e}")

    async def on_unmount(self) -> None:
        await self.mpv.shutdown()
        if self.db is not None:
            self.db.deinit()

    async def action_quit(self) -> None:
        await self.mpv.shutdown()
        if self.db is not None:
            self.db.deinit()
        self.exit()

    def set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    async def _handle_eof(self) -> None:
        await self.play_next()

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

            msg = f"Added {len(new_songs)} song(s) from {folder}"
            if duplicates:
                msg += f" ({duplicates} duplicate(s) skipped)"
            self.set_status(msg)

        self.push_screen(AddFolderScreen(), handle_result)

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
            self.displayed = [s for s in self.songs if query in (getattr(s, self.search_field) or "").lower()]
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
        index = list_view.index if list_view.index is not None else self._default_index()
        song = self._song_at(index)
        if song is not None:
            await self.play_song(song)

    async def play_song(self, song: Song) -> None:
        try:
            await self.mpv.load(str(song.path))
        except MPVError as e:
            self.set_status(f"Error playing {song.title}: {e}")
            return
        self.current_index = self.songs.index(song)
        self.paused = False
        list_view = self.query_one("#library", ListView)
        list_view.index = self._row_songs.index(song) if song in self._row_songs else None
        self.set_status(f"Playing: {song.title}")
        self.query_one(SongInfoPanel).show_song(song)
        self.run_worker(self.query_one(AlbumArt).show_song(song.path), exclusive=True, group="art")

    async def play_next(self) -> None:
        if self.current_index is None:
            return
        next_index = self.current_index + 1
        if next_index < len(self.songs):
            await self.play_song(self.songs[next_index])
        else:
            self.current_index = None
            self.set_status("Playback finished.")

    async def action_toggle_pause(self) -> None:
        if self.current_index is None:
            return
        self.paused = not self.paused
        await self.mpv.set_pause(self.paused)
        state = "Paused" if self.paused else "Playing"
        self.set_status(f"{state}: {self.songs[self.current_index].title}")

    async def action_stop(self) -> None:
        await self.mpv.stop()
        self.current_index = None
        self.paused = False
        self.set_status("Stopped.")
        self.query_one(AlbumArt).clear()
        self.query_one(SongInfoPanel).clear()

    async def action_volume_up(self) -> None:
        self.volume = min(1.0, self.volume + 0.1)
        await self.mpv.set_volume(self.volume)
        self.set_status(f"Volume: {int(self.volume * 100)}%")

    async def action_volume_down(self) -> None:
        self.volume = max(0.0, self.volume - 0.1)
        await self.mpv.set_volume(self.volume)
        self.set_status(f"Volume: {int(self.volume * 100)}%")

    @on(ListView.Selected)
    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        song = self._song_at(self.query_one("#library", ListView).index)
        if song is not None:
            await self.play_song(song)


def main() -> None:
    MusicPlayerApp().run()


if __name__ == "__main__":
    main()
