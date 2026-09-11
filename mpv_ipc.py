from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from typing import Any, Awaitable, Callable


class MPVError(RuntimeError):
    pass


class MPVPlayer:
    """Drives an `mpv` subprocess over its JSON IPC socket."""

    def __init__(self, on_eof: Callable[[], Awaitable[None]] | None = None) -> None:
        self._on_eof = on_eof
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
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "mpv",
                "--idle=yes",
                "--no-video",
                "--no-terminal",
                "--no-config",
                "--keep-open=yes",
                f"--input-ipc-server={self.socket_path}",
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
        await self.command("observe_property", 1, "eof-reached")

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
            elif data.get("event") == "property-change" and data.get("name") == "eof-reached":
                if data.get("data") and self._on_eof is not None:
                    asyncio.create_task(self._on_eof())

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

    async def load(self, path: str) -> None:
        await self.command("loadfile", path, "replace")
        await self.command("set_property", "pause", False)

    async def set_pause(self, paused: bool) -> None:
        await self.command("set_property", "pause", paused)

    async def stop(self) -> None:
        await self.command("stop")

    async def set_volume(self, volume: float) -> None:
        await self.command("set_property", "volume", max(0.0, min(100.0, volume * 100)))

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
