from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from typing import Any, Awaitable, Callable


class MPVError(RuntimeError):
    pass


class MPVPlayer:
    """Drives an `mpv` subprocess over its JSON IPC socket, using mpv's own playlist so it
    can advance tracks (and respond to OS media keys) on its own."""

    def __init__(
        self,
        on_playlist_pos_change: Callable[[int], Awaitable[None]] | None = None,
        on_pause_change: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        self._on_playlist_pos_change = on_playlist_pos_change
        self._on_pause_change = on_pause_change
        self.socket_path = os.path.join(
            tempfile.gettempdir(), f"tui-player-mpv-{uuid.uuid4().hex}.sock"
        )
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}

    async def start(self) -> None:
        args = [
            "mpv",
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--no-config",
            f"--input-ipc-server={self.socket_path}",
        ]
        if sys.platform == "darwin":
            # Keep it out of the Dock/app-switcher while still eligible for the
            # system's Now Playing / media-key routing.
            args.append("--macos-app-activation-policy=accessory")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise MPVError("mpv executable not found. Install it with `brew install mpv`.") from e

        for _ in range(100):
            if os.path.exists(self.socket_path):
                break
            await asyncio.sleep(0.05)
        else:
            raise MPVError("Timed out waiting for mpv IPC socket.")

        for _ in range(50):
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
                break
            except (ConnectionRefusedError, FileNotFoundError):
                await asyncio.sleep(0.05)
        else:
            raise MPVError("Could not connect to mpv IPC socket.")

        self._reader_task = asyncio.create_task(self._read_loop())
        await self.command("observe_property", 1, "playlist-pos")
        await self.command("observe_property", 2, "pause")

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            try:
                line = await self._reader.readline()
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            if not line:
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "request_id" in data:
                fut = self._pending.pop(data["request_id"], None)
                if fut is not None and not fut.done():
                    if data.get("error") not in (None, "success"):
                        fut.set_exception(MPVError(str(data.get("error"))))
                    else:
                        fut.set_result(data.get("data"))
            elif data.get("event") == "property-change":
                name = data.get("name")
                if name == "playlist-pos" and self._on_playlist_pos_change is not None:
                    pos = data.get("data")
                    asyncio.create_task(self._on_playlist_pos_change(pos if pos is not None else -1))
                elif name == "pause" and self._on_pause_change is not None:
                    asyncio.create_task(self._on_pause_change(bool(data.get("data"))))

    async def command(self, *args: Any) -> Any:
        if self._writer is None:
            raise MPVError("mpv is not running.")
        request_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        payload = json.dumps({"command": list(args), "request_id": request_id}) + "\n"
        self._writer.write(payload.encode())
        await self._writer.drain()
        return await fut

    async def playlist_append(self, path: str, play: bool = False) -> None:
        await self.command("loadfile", path, "append-play" if play else "append")

    async def playlist_clear(self) -> None:
        await self.command("playlist-clear")

    async def playlist_remove(self, index: int) -> None:
        await self.command("playlist-remove", index)

    async def playlist_play_index(self, index: int) -> None:
        await self.command("set_property", "playlist-pos", index)

    async def playlist_shuffle(self) -> None:
        await self.command("playlist-shuffle")

    async def playlist_unshuffle(self) -> None:
        await self.command("playlist-unshuffle")

    async def get_playlist(self) -> list[dict]:
        try:
            return await self.command("get_property", "playlist") or []
        except MPVError:
            return []

    async def get_playlist_pos(self) -> int:
        try:
            pos = await self.command("get_property", "playlist-pos")
            return pos if pos is not None and pos >= 0 else -1
        except MPVError:
            return -1

    async def set_loop_playlist(self, mode: str) -> None:
        await self.command("set_property", "loop-playlist", mode)

    async def set_loop_file(self, mode: str) -> None:
        await self.command("set_property", "loop-file", mode)

    async def set_pause(self, paused: bool) -> None:
        await self.command("set_property", "pause", paused)

    async def stop(self) -> None:
        await self.command("stop")

    async def set_volume(self, volume: float) -> None:
        await self.command("set_property", "volume", max(0.0, min(100.0, volume * 100)))

    async def seek_relative(self, seconds: float) -> None:
        await self.command("seek", seconds, "relative")

    async def seek_absolute(self, seconds: float) -> None:
        await self.command("seek", seconds, "absolute")

    async def get_time_pos(self) -> float | None:
        try:
            return await self.command("get_property", "time-pos")
        except MPVError:
            return None

    async def get_duration(self) -> float | None:
        try:
            return await self.command("get_property", "duration")
        except MPVError:
            return None

    async def shutdown(self) -> None:
        if self._writer is not None:
            try:
                await self.command("quit")
            except MPVError:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
        if os.path.exists(self.socket_path):
            try:
                os.remove(self.socket_path)
            except OSError:
                pass
