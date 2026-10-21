"""A lock the operating system holds for us on an open file, freed the moment the process ends.

Nothing here checks process ids: a window that crashes or is killed loses its locks with it, so a
lock can never be left behind, half-written, or held by a reused pid. The lock files themselves are
never deleted -- deleting one while another process has it open would let two processes "hold" it.
"""
from __future__ import annotations

import json
import os
import socket
import sys

LOCK_AT = 1 << 20  # the locked byte sits past the owner note, so the note stays readable


def try_lock(path, note=True):
    """An open file descriptor holding the lock on `path`, or None when another process has it.
    note=False takes it without writing who has it (for a quick "is anyone holding this?" probe)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    if not note:
        return fd
    who = json.dumps({"pid": os.getpid(), "host": socket.gethostname()}).encode()
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, who.ljust(256))  # who has it, for a person looking at the file; never trusted
    return fd


def held_elsewhere(path):
    """Does another process hold the lock on `path`? Checks without keeping it."""
    fd = try_lock(path, note=False)
    if fd is None:
        return True
    unlock(fd)
    return False


def unlock(fd):
    """Let go of a lock from try_lock()."""
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)
