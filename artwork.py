from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.flac import Picture
from mutagen.id3 import APIC
from mutagen.mp4 import MP4Cover

COVER_FILENAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "album.jpg", "album.jpeg", "album.png",
)


def _extract_embedded_art(path: Path) -> bytes | None:
    try:
        audio = MutagenFile(path)
    except Exception:
        return None
    if audio is None:
        return None

    tags = audio.tags
    if tags is None:
        return None

    for key in tags.keys() if hasattr(tags, "keys") else []:
        if key.startswith("APIC"):
            value = tags[key]
            if isinstance(value, APIC):
                return value.data

    if "covr" in tags:
        covers = tags["covr"]
        if covers:
            cover = covers[0]
            if isinstance(cover, (MP4Cover, bytes, bytearray)):
                return bytes(cover)

    pictures = getattr(audio, "pictures", None)
    if pictures:
        pic = pictures[0]
        if isinstance(pic, Picture):
            return pic.data

    return None


def find_cover_bytes(song_path: Path) -> bytes | None:
    """Look for embedded artwork first, then a cover image file next to the song."""
    art = _extract_embedded_art(song_path)
    if art:
        return art

    folder = song_path.parent
    for name in COVER_FILENAMES:
        candidate = folder / name
        if candidate.is_file():
            try:
                return candidate.read_bytes()
            except OSError:
                return None
    return None


@dataclass
class SongInfo:
    title: str
    artist: str | None
    album: str | None
    duration: float | None
    file_type: str
    bitrate: int | None
    sample_rate: int | None
    bit_depth: int | None
    channels: int | None


def _first_tag(tags: Any, keys: tuple[str, ...]) -> str | None:
    """Look up a tag by either its "easy" name (e.g. "title") or raw ID3 frame id (e.g. "TIT2")."""
    for key in keys:
        if key not in tags:
            continue
        value = tags[key]
        item = value[0] if isinstance(value, list) else value
        text = getattr(item, "text", None)
        if text:
            return str(text[0])
        return str(item)
    return None


def get_song_info(song_path: Path) -> SongInfo:
    title = song_path.stem
    artist = album = None
    duration = bitrate = sample_rate = bit_depth = channels = None
    file_type = song_path.suffix.lstrip(".").upper() or "?"

    try:
        audio = MutagenFile(song_path, easy=True)
    except Exception:
        audio = None

    if audio is not None:
        tags = audio.tags
        if tags:
            title = _first_tag(tags, ("title", "TIT2")) or title
            artist = _first_tag(tags, ("artist", "TPE1"))
            album = _first_tag(tags, ("album", "TALB"))

        info = audio.info
        if info is not None:
            duration = getattr(info, "length", None)
            sample_rate = getattr(info, "sample_rate", None)
            channels = getattr(info, "channels", None)
            bit_depth = getattr(info, "bits_per_sample", None)
            raw_bitrate = getattr(info, "bitrate", None)
            if raw_bitrate:
                bitrate = round(raw_bitrate / 1000)

    return SongInfo(
        title=title,
        artist=artist,
        album=album,
        duration=duration,
        file_type=file_type,
        bitrate=bitrate,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        channels=channels,
    )


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def format_bitrate(kbps: int | None) -> str:
    return f"{kbps} kbps" if kbps else "? kbps"


def format_sample_rate(hz: int | None) -> str:
    return f"{hz / 1000:.1f} kHz" if hz else "? kHz"


def format_bit_depth(bits: int | None) -> str:
    return f"{bits}-bit" if bits else "?-bit"


async def render_with_chafa(image_bytes: bytes, width: int, height: int) -> str | None:
    """Render image bytes to ANSI art via the `chafa` CLI. Returns None if chafa is unavailable."""
    if width <= 0 or height <= 0:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "chafa",
            f"--size={width}x{height}",
            "--format=symbols",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None

    stdout, _ = await proc.communicate(input=image_bytes)
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace")
