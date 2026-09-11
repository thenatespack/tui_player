from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_REPO = Path(os.environ.get("SPLAYLITE_REPO", "~/splaylite")).expanduser()
VENDORED_DIR = Path(__file__).parent / "lib"

_LIB_NAMES = {
    "darwin": "libsplaylite.dylib",
    "win32": "splaylite.dll",
}


def default_lib_path() -> Path:
    override = os.environ.get("SPLAYLITE_LIB")
    if override:
        return Path(override).expanduser()
    name = _LIB_NAMES.get(sys.platform, "libsplaylite.so")
    vendored = VENDORED_DIR / name
    if vendored.is_file():
        return vendored
    return DEFAULT_REPO / "zig-out" / "lib" / name


class SplayLiteError(RuntimeError):
    pass


class SplayLite:
    """Thin ctypes binding to splaylite's C-API (see splaylite/src/c_api.zig)."""

    def __init__(self, lib_path: Path | None = None) -> None:
        self.lib_path = lib_path or default_lib_path()
        self._lib = ctypes.CDLL(str(self.lib_path))
        self._lib.splaylite_init.restype = ctypes.c_int
        self._lib.splaylite_deinit.restype = None
        self._lib.splaylite_query.argtypes = [ctypes.c_char_p]
        self._lib.splaylite_query.restype = ctypes.c_char_p
        self._initialized = False

    def init(self) -> None:
        if self._initialized:
            return
        if self._lib.splaylite_init() != 0:
            raise SplayLiteError("splaylite_init failed")
        self._initialized = True

    def deinit(self) -> None:
        if not self._initialized:
            return
        self._lib.splaylite_deinit()
        self._initialized = False

    def query(self, sql: str) -> dict[str, Any]:
        if not self._initialized:
            self.init()
        raw = self._lib.splaylite_query(sql.encode("utf-8"))
        if raw is None:
            raise SplayLiteError("splaylite_query: database not initialized")
        result = json.loads(raw.decode("utf-8"))
        if result.get("kind") == "error":
            raise SplayLiteError(f"{result.get('message')} (sql: {sql!r})")
        return result


def sql_string(value: str | None) -> str:
    """Render a Python string (or None) as a SQL literal, escaping quotes."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"
