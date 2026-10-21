"""The save as the player is editing it: a draft that only reaches the real file when they press Save.

Edits land in a draft per save file. Each draft is kept in a file in the tool's cache
(<cache>/drafts/<key>.json, written atomically after every edit), so unsaved edits survive the
app closing or crashing and are picked up again next time. Memory only caches drafts.

An operating-system lock on <key>.lock (webapp/oslock.py) stops two Shipwright windows editing
the same save. The process holding it may edit; any other one only gets a read-only view of the
file on disk (edits answer HTTP 423). The OS frees the lock when its process ends, however it ends.

Locking order: a save's edit lock (EDIT_LOCKS, held for the whole of an edit, a save or a load)
before DRAFTS_LOCK (held only for quick bookkeeping, never across file I/O).

Saving checks that the game isn't running, that the player saves what they reviewed (rev =
"<gen>:<version>"), and that the file on disk is still the one the draft started from. It backs
the file up, writes it atomically, then reads it back to make sure it holds exactly the draft.
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import re
import shutil
import threading
import uuid

from flask import g, has_request_context, request

from nms_save import gamefiles, save

from webapp import common, oslock


class SaveLocked(ValueError):
    pass


class UnsavedEdits(ValueError):
    pass


class ReadBackError(RuntimeError):
    def __init__(self, message, backup):
        super().__init__(message)
        self.backup = backup


LOCKED_MSG = "This save is open in another Shipwright window. Close it there to edit it here."
STALE_MSG = ("Your save file changed on disk since you started editing (did the game save?). "
             "Nothing was written. Discard your unsaved changes to load the new file.")
RACE_MSG = "Another change landed at the same moment -- nothing was changed, try again."
UNSAVED_MSG = "You have unsaved changes -- save or discard them first."

# In-memory cache of drafts by _key(path): {"path", "opened": OpenSave, "base", "gen", "version",
# "saved_version", "resumed"}. Unsaved ones are on disk before they are here, so dropping one loses nothing.
DRAFTS: dict[str, dict] = {}
DRAFTS_LOCK = threading.RLock()
DRAFTS_KEEP = 2

HELD: dict[str, int] = {}  # key -> open descriptor holding that save's OS lock
SELECTED = {"key": None}   # the save the page has open: keep its lock even when it has no edits
EDIT_LOCKS: dict[str, threading.RLock] = {}
SESSION_GENS: set[str] = set()  # drafts started by this process (anything else was left from before)
SAVING = [0]  # writes to a real save in progress (the close-window question must not cut one off)


@contextlib.contextmanager
def _saving():
    with DRAFTS_LOCK:
        SAVING[0] += 1
    try:
        yield
    finally:
        with DRAFTS_LOCK:
            SAVING[0] -= 1


def saving():
    """Is this process writing a save right now? Lock-free, for the window's own thread."""
    return SAVING[0] > 0


def _key(path):
    return os.path.normcase(os.path.realpath(path))


def _dir():
    d = os.path.join(gamefiles.CACHE, "drafts")
    os.makedirs(d, exist_ok=True)
    return d


def _file(key, ext):
    return os.path.join(_dir(), hashlib.sha1(key.encode("utf-8", "surrogateescape")).hexdigest()[:16] + ext)


def _write_json(path, data):
    tmp = path + ".tmp"
    text = json.dumps(data, separators=(",", ":"))  # one write: json.dump to a file is several times slower
    with open(tmp, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(text)
    os.replace(tmp, path)


def _copy(tree):
    """A private copy of a save tree. Trees are plain JSON, and this beats copy.deepcopy several times over."""
    return json.loads(json.dumps(tree))


# --- which process is editing a save ---------------------------------------------------------

def claim(key):
    """Take this save's lock unless another process has it. True when this process may edit it."""
    with DRAFTS_LOCK:
        if key in HELD:
            return True
        fd = oslock.try_lock(_file(key, ".lock"))
        if fd is None:
            return False
        HELD[key] = fd
        DRAFTS.pop(key, None)  # another window may have edited it while we didn't hold it: re-read its draft
        return True


def release(key):
    with DRAFTS_LOCK:  # unlock inside: a claim() from this process must not see it half let go
        fd = HELD.pop(key, None)
        DRAFTS.pop(key, None)  # once let go, another window may edit it: never trust this copy again
        if fd is not None:
            oslock.unlock(fd)


@atexit.register
def release_all():
    for key in list(HELD):
        release(key)


def _release_if_idle(key):
    """Let go of a save the page has moved away from, unless it still has unsaved edits or is busy."""
    if key == SELECTED["key"] or os.path.exists(_file(key, ".json")):
        return
    lock = _edit_lock(key)
    if lock.acquire(blocking=False):  # an edit in progress keeps it
        try:
            if not os.path.exists(_file(key, ".json")):
                release(key)
        finally:
            lock.release()


def select(path):
    """The page has this save open: hold its lock and drop the locks of other saves with no edits."""
    key = _key(path)
    with DRAFTS_LOCK:
        SELECTED["key"] = key
        others = [k for k in HELD if k != key]
    claim(key)
    for other in others:
        _release_if_idle(other)


# --- the drafts themselves -------------------------------------------------------------------

def _read_stable(path):
    """(bytes, base) of the file, read while it was not changing: a save landing mid-read would
    otherwise pair the new file's stamp with the old file's bytes and hide that it changed."""
    for _ in range(5):
        before = os.stat(path)
        with open(path, "rb") as f:
            raw = f.read()
        after = os.stat(path)
        if (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size) == (after.st_mtime_ns, len(raw)):
            return raw, {"mtime_ns": after.st_mtime_ns, "size": after.st_size,
                         "sha256": hashlib.sha256(raw).hexdigest()}
    raise ValueError("The save file keeps changing (is the game saving?) -- try again in a moment.")


def disk_changed(path, base):
    """Is the file on disk no longer the one `base` describes? Hashes only when the stamp moved."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return True
    if (st.st_mtime_ns, st.st_size) == (base["mtime_ns"], base["size"]):
        return False
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest() != base["sha256"]


def _dirty(d):
    return d["version"] != d["saved_version"]


def _rev(d):
    return f"{d['gen']}:{d['version']}"


def _clear_leftover_tmp(path):
    """A write cut off mid-way (the app closed during a save) can leave save.write_atomic's temp file
    beside the save. The save itself is whole either way; the stray temp file is just litter."""
    with contextlib.suppress(OSError):
        os.remove(save.tmp_path_for(path))


def _from_disk(path):
    _clear_leftover_tmp(path)
    raw, base = _read_stable(path)
    gen = uuid.uuid4().hex
    SESSION_GENS.add(gen)
    return {"path": path, "opened": save.from_bytes(path, raw), "base": base,
            "gen": gen, "version": 0, "saved_version": 0, "resumed": False}


META = ("path", "base", "gen", "version", "saved_version")


def _meta(d):
    return {k: d[k] for k in META}


def _persist(d):
    key = _key(d["path"])
    o = d["opened"]
    _write_json(_file(key, ".json"), {**_meta(d), "chunked": o._chunked, "trailing_nul": o._trailing_nul,
                                      "tree": o.readable})
    _write_json(_file(key, ".meta"), _meta(d))  # small copy for listing drafts without loading trees


def _unpersist(key):
    for ext in (".meta", ".json"):  # meta first: a crash in between must not leave a listed ghost
        with contextlib.suppress(FileNotFoundError):
            os.remove(_file(key, ext))


def _from_file(key):
    """A draft left from earlier (this run or a previous one), or None."""
    try:
        with open(_file(key, ".json"), encoding="utf-8", errors="surrogateescape") as f:
            data = json.load(f)
        d = {k: data[k] for k in META}
        if not isinstance(data["tree"], dict) or not isinstance(d["base"], dict) or not {"mtime_ns", "size", "sha256"} <= set(d["base"]):
            raise TypeError("not a draft")
        d["version"], d["saved_version"] = int(d["version"]), int(d["saved_version"])
        d["opened"] = save.OpenSave(path=d["path"], readable=data["tree"], _chunked=bool(data["chunked"]),
                                    _trailing_nul=int(data["trailing_nul"]))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):  # unreadable draft: keep it aside rather than lose it silently
        with contextlib.suppress(OSError):
            os.replace(_file(key, ".json"), _file(key, ".broken"))
        _unpersist(key)
        return None
    d["resumed"] = d["gen"] not in SESSION_GENS
    return d


def _cache(key, d):
    with DRAFTS_LOCK:
        DRAFTS.pop(key, None)
        while len(DRAFTS) >= DRAFTS_KEEP:  # memory only: unsaved drafts are safe in their files
            del DRAFTS[next(iter(DRAFTS))]
        DRAFTS[key] = d  # most recently used last
    return d


def _current(path):
    """The draft as it stands (memory, then draft file), or None. Never reloads from disk.
    Call with the save's edit lock held."""
    key = _key(path)
    with DRAFTS_LOCK:
        d = DRAFTS.get(key)
    if d is None:
        d = _from_file(key)
        if d is not None:
            _cache(key, d)
    return d


def _load(path):
    """The draft to show/edit: the current one, or a fresh one from disk when there is none or a
    clean one whose file moved on. Call with the save's edit lock held."""
    d = _current(path)
    if d and not _dirty(d) and disk_changed(path, d["base"]):
        d = None
    if d is None:
        d = _cache(_key(path), _from_disk(path))
    return d


# --- what the routes use ---------------------------------------------------------------------

def _edit_lock(key):
    with DRAFTS_LOCK:
        return EDIT_LOCKS.setdefault(key, threading.RLock())


@contextlib.contextmanager
def editing(path):
    """Hold this save's edit lock (for edits made outside a request, e.g. the design install job)."""
    lock = _edit_lock(_key(path))
    with lock:
        yield


def release_edit_locks(_exc=None):
    """teardown_request: let go of the edit locks open_save() took for this request."""
    for lock in reversed(g.pop("edit_locks", [])):
        lock.release()


def check_path(path):
    if not path or not os.path.exists(path):
        raise ValueError("Missing or invalid save path")


def open_save(path):
    """The save as the player is editing it.

    GET requests get the draft tree itself (read it, don't change it); everything else gets a
    private copy, so a route that fails halfway can't leave a half-made change in the draft, and
    holds the save's edit lock until the request ends, so two edits never race. When another
    Shipwright window is editing this save, GETs see the file on disk and edits are refused."""
    check_path(path)
    key = _key(path)
    readonly = has_request_context() and request.method == "GET"
    lock = _edit_lock(key)
    if readonly:
        if not claim(key):
            return save.open_save(path)
        with DRAFTS_LOCK:
            d = DRAFTS.get(key)
        if d is None or (not _dirty(d) and disk_changed(path, d["base"])):
            with lock:
                d = _load(path)
        src = d["opened"]
        opened = save.OpenSave(path=src.path, readable=src.readable, _chunked=src._chunked,
                               _trailing_nul=src._trailing_nul)
        opened.base = (d["gen"], d["version"])
        return opened
    lock.acquire()
    if has_request_context():
        g.setdefault("edit_locks", []).append(lock)  # held until the request ends
    try:
        if not claim(key):
            raise SaveLocked(LOCKED_MSG)
        d = _load(path)
        src = d["opened"]
        opened = save.OpenSave(path=src.path, readable=_copy(src.readable), _chunked=src._chunked,
                               _trailing_nul=src._trailing_nul)
        opened.base = (d["gen"], d["version"])
        return opened
    finally:
        if not has_request_context():
            lock.release()  # outside a request the caller holds editing() itself


def write(opened):
    """Put an edit into the draft (and its file). Nothing reaches the save until save_draft()."""
    key = _key(opened.path)
    with _edit_lock(key):
        if not claim(key):
            raise SaveLocked(LOCKED_MSG)
        d = _current(opened.path)
        if d is None or (d["gen"], d["version"]) != getattr(opened, "base", None):
            raise ValueError(RACE_MSG)
        if disk_changed(opened.path, d["base"]):
            if _dirty(d):
                raise ValueError(STALE_MSG)
            with DRAFTS_LOCK:
                DRAFTS.pop(key, None)  # reload the new file next time
            raise ValueError("Your save file changed on disk (did the game save?) -- nothing was changed, try again.")
        new = {**d, "opened": save.OpenSave(path=d["path"], readable=opened.readable, _chunked=d["opened"]._chunked,
                                            _trailing_nul=d["opened"]._trailing_nul),
               "version": d["version"] + 1}
        _persist(new)  # on disk first: if this fails, memory still matches the file
        _cache(key, new)
    return None


def state(path):
    """Cheap status for the Save bar; also tells this process the page has this save open."""
    check_path(path)
    key = _key(path)
    select(path)
    out = {"dirty": False, "changes": 0, "rev": None, "stale": False, "resumed": False, "locked": None,
           "file": os.path.basename(path), "game_running": common.game_running_cached()}
    if key not in HELD:
        out["locked"] = {"message": LOCKED_MSG}
        return out
    with DRAFTS_LOCK:
        d = DRAFTS.get(key)
    if d is None and os.path.exists(_file(key, ".json")):
        with _edit_lock(key):
            d = _current(path)
    if d:
        out.update(dirty=_dirty(d), changes=d["version"] - d["saved_version"], rev=_rev(d),
                   resumed=d["resumed"] and _dirty(d), stale=_dirty(d) and disk_changed(path, d["base"]))
    return out


def listing():
    """Every save with unsaved edits, including drafts left from a previous session or another window."""
    out, seen = [], set()
    for name in sorted(os.listdir(_dir())):
        if not name.endswith(".meta"):
            continue
        try:
            with open(os.path.join(_dir(), name), encoding="utf-8", errors="surrogateescape") as f:
                m = json.load(f)
            key = _key(m["path"])
            with DRAFTS_LOCK:
                d = DRAFTS.get(key) if key in HELD else None  # memory is only current for saves we hold
            if d:
                m = _meta(d)
            if key in seen or m["version"] == m["saved_version"] or not os.path.exists(m["path"]):
                continue
            seen.add(key)
            # Only look: holding every listed save would show them all as locked to another window.
            locked = key not in HELD and oslock.held_elsewhere(_file(key, ".lock"))
            out.append({"path": m["path"], "file": os.path.basename(m["path"]),
                        "changes": m["version"] - m["saved_version"], "stale": disk_changed(m["path"], m["base"]),
                        "resumed": d["resumed"] if d else m["gen"] not in SESSION_GENS,
                        "locked": {"message": LOCKED_MSG} if locked else None})
        except (OSError, ValueError, KeyError, TypeError):
            continue  # one odd file must not hide the rest
    return out


def _mine(path):
    if not claim(_key(path)):
        raise SaveLocked(LOCKED_MSG)


def review(path):
    """Everything the draft would change in the file on disk."""
    from webapp import summary
    check_path(path)
    with _edit_lock(_key(path)):
        _mine(path)
        d = _current(path)
        if not d or not _dirty(d):
            return {"rev": _rev(d) if d else None, "file": os.path.basename(path), "changes": [], "total": 0,
                    "stale": False, "missing": True}
        tree, rev, stale = d["opened"].readable, _rev(d), disk_changed(path, d["base"])
    changes, total = summary.describe(save.open_save(path).readable, tree)
    return {"rev": rev, "file": os.path.basename(path), "changes": changes, "total": total, "stale": stale,
            "missing": False}


def _read_back(path, raw):
    """The JSON text in the bytes just read back from the save (a seam for tests)."""
    from nms_save import codec
    return codec.decode(raw)[0]


def save_draft(path, rev):
    """Write the draft to the file, if it is still what the player reviewed and the file hasn't moved on."""
    check_path(path)
    common.ensure_game_closed()
    key = _key(path)
    with _edit_lock(key):
        _mine(path)
        d = _current(path)
        if not d or not _dirty(d):
            raise ValueError("There are no unsaved changes to save.")
        if rev != _rev(d):
            raise ValueError("Your changes changed since you reviewed them -- review again.")
        if disk_changed(path, d["base"]):
            raise ValueError(STALE_MSG)
        common.ensure_game_closed()  # again: the checks above can take a moment
        with _saving():
            backup, text = d["opened"].write(return_text=True)
            # The file is ours now: record that before anything else can fail, so neither a retry nor a
            # restart calls it stale. One stable read gives both the new base and the bytes to check.
            try:
                raw, base = _read_stable(path)
                d = {**d, "base": base}
                _persist(d)
                _cache(key, d)
                back = _read_back(path, raw)
            except Exception:
                back = None
        name = os.path.basename(backup)
        if back is None:
            raise ReadBackError(f"Saved, but Shipwright couldn't check the file it just wrote. Your previous save is "
                                f"in {name}. Your edits are still here unsaved; try Save again, or restore that "
                                "backup if the game won't load the save.", backup)
        if back != text:  # exactly the text we encoded, so the file holds exactly the draft
            raise ReadBackError(f"Your save was written but does not read back the same. Your edits are still "
                                f"here unsaved; the file from before is backed up as {name}. "
                                "Try Save again, or restore that backup.", backup)
        _cache(key, {**d, "saved_version": d["version"], "resumed": False})
        _unpersist(key)
        _release_if_idle(key)
    return {"ok": True, "backup": backup, "file": os.path.basename(path)}


def discard(path, rev=None):
    """Throw the unsaved edits away. With `rev`, only if they are still the ones the player saw."""
    check_path(path)
    key = _key(path)
    with _edit_lock(key):
        _mine(path)
        if rev is not None:
            d = _current(path)
            if d and _dirty(d) and _rev(d) != rev:
                raise ValueError("Your changes changed since you looked at them -- nothing was thrown away.")
        with DRAFTS_LOCK:
            DRAFTS.pop(key, None)
        _unpersist(key)
        _release_if_idle(key)


def dirty_any():
    """Does this process hold unsaved edits anywhere (for the close-window question)? Takes no locks:
    it runs on the window's own thread, which must never wait behind a save."""
    return any(os.path.exists(_file(k, ".json")) for k in list(HELD))


def backup_for(path, src):
    """`src` if it really is one of this save's backups (same folder, <save name>.<stamp>.bak), else ValueError."""
    if not src or not os.path.isfile(src):
        raise ValueError("Unknown backup file")
    real, folder = os.path.realpath(src), os.path.dirname(os.path.realpath(path))
    pattern = re.escape(os.path.basename(os.path.realpath(path))) + r"\.\d{8}-\d{6}-\d+(-\d+)?\.bak"
    if (os.path.normcase(os.path.dirname(real)) != os.path.normcase(folder)
            or not re.fullmatch(pattern, os.path.basename(real), re.IGNORECASE if os.name == "nt" else 0)):
        raise ValueError("Unknown backup file")
    return real


def restore(path, src):
    """Replace a save with one of its backups. Returns (backup bytes, commit) for the preview: commit(stamp)
    writes exactly those bytes, provided the save is still the file the preview was made from."""
    check_path(path)
    src = backup_for(path, src)
    key = _key(path)

    def ensure():
        _mine(path)
        d = _current(path)
        if d and _dirty(d):
            raise UnsavedEdits(UNSAVED_MSG)

    with _edit_lock(key):
        ensure()
    with open(src, "rb") as f:
        data = f.read()  # restore exactly what the preview shows, even if the backup changes later

    def commit(stamp):
        common.ensure_game_closed("No Man's Sky is running -- quit the game first.")
        with _edit_lock(key):
            ensure()
            if not os.path.exists(path) or common.file_stamp(path) != stamp:
                raise ValueError("Nothing was written: your save changed since this summary was made "
                                 "(did the game save?). Do it again to see a fresh summary.")
            safety = save.backup_path_for(path)
            with _saving():
                shutil.copy2(path, safety)
                save.write_atomic(path, data)
            with DRAFTS_LOCK:
                DRAFTS.pop(key, None)  # the file was replaced: edit the restored one from now on
            _unpersist(key)
        return {"safety_copy": safety}

    return data, commit
