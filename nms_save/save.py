"""High-level open/save for a single .hg file: decode + deobfuscate in,
obfuscate + encode out, with a mandatory timestamped backup on every write.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime

from . import codec, mapping


def backup_path_for(path: str) -> str:
    """Where write() (or anyone else) should put a timestamped backup of `path`.

    Includes microseconds: two backups of the same file within the same
    wall-clock second must never collide on a filename -- a collision
    would silently overwrite one backup with another (or, in an undo
    flow, with the very state being undone).
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    base = os.path.basename(path)
    candidate = os.path.join(os.path.dirname(path), f"{base}.{stamp}.bak")
    # Belt-and-suspenders: if it *still* somehow exists, disambiguate further.
    n = 1
    while os.path.exists(candidate):
        candidate = os.path.join(os.path.dirname(path), f"{base}.{stamp}-{n}.bak")
        n += 1
    return candidate


@dataclass
class OpenSave:
    path: str
    readable: dict  # deobfuscated JSON tree -- mutate this
    _chunked: bool
    _trailing_nul: int

    def backup_path(self) -> str:
        return backup_path_for(self.path)

    def write(self, *, backup: bool = True, return_text: bool = False):
        """Write self.readable back to self.path.

        Returns the backup file path (or None if backup=False). Always
        backs up by default -- pass backup=False only if you already made
        one yourself. With return_text, returns (backup path, the JSON text
        written) so a caller can check the file reads back as exactly that.
        """
        backup_path = None
        if backup:
            backup_path = self.backup_path()
            shutil.copy2(self.path, backup_path)

        obfuscated = mapping.obfuscate(self.readable)
        text = json.dumps(obfuscated, separators=(",", ":"))
        raw = codec.encode(text, self._chunked, self._trailing_nul)

        write_atomic(self.path, raw)

        return (backup_path, text) if return_text else backup_path


def tmp_path_for(path: str) -> str:
    """The temp file write_atomic() writes beside `path` before swapping it in."""
    return path + ".tmp"


def write_atomic(path: str, raw: bytes) -> None:
    """Replace `path` with `raw` in one step: write a temp file beside it, then swap it in.

    A failure halfway (full disk, a file lock) leaves the old file whole instead of a truncated save.
    """
    tmp = tmp_path_for(path)
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def open_save(path: str) -> OpenSave:
    with open(path, "rb") as f:
        raw = f.read()
    return from_bytes(path, raw)


def from_bytes(path: str, raw: bytes) -> OpenSave:
    """Decode save bytes already read from `path` (so a caller can hash exactly what it decoded)."""
    text, chunked, trailing_nul = codec.decode(raw)
    data = json.loads(text)
    readable = mapping.deobfuscate(data)

    return OpenSave(
        path=path,
        readable=readable,
        _chunked=chunked,
        _trailing_nul=trailing_nul,
    )
