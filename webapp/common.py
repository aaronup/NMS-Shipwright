"""Shared pieces the route modules use: paths, labels, opening and writing saves, the job list."""
from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import g, has_request_context, request


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nms_procgen import ROOTS, textures  # noqa: E402
from nms_save import gamefiles, gamepath, gameversion, items, quests, save  # noqa: E402


STATIC = os.path.join(os.path.dirname(__file__), "static")


SHIP_LABELS = {"fighter": "Fighter", "hauler": "Hauler", "explorer": "Explorer", "shuttle": "Shuttle",
               "exotic": "Exotic", "living": "Living Ship", "solar": "Solar", "sentinel": "Sentinel",
               "freighter": "Freighter", "capital_freighter": "Capital Freighter"}


SHIP_MODELS = {k: "MODELS/" + v[len("models/"):].upper().replace(".DESCRIPTOR.MBIN", ".SCENE.MBIN")
               for k, v in ROOTS.items() if k not in ("freighter", "capital_freighter")}


SPECIAL_SHIPS = {}
_special_dir = ROOT / "data" / "special_ships"
if _special_dir.exists():
    for _f in sorted(_special_dir.glob("*.json")):
        with open(_f, encoding="utf-8") as _sf:
            _entry = json.load(_sf)
        SPECIAL_SHIPS[_f.stem] = {"label": _entry.get("Name") or _f.stem, "entry": _entry}


with open(ROOT / "data" / "palettes" / "customisation.json") as _f:
    PALETTES = {k: v["colours"] for k, v in json.load(_f)["palettes"].items() if v["num_colours_enum"] == 5}

@functools.lru_cache(maxsize=None)
def part_names(ship):
    path = ROOT / "nms_procgen" / "names" / f"{ship}.json"
    names = {}
    if path.exists():
        for row in json.load(open(path)):
            names.setdefault(row["id"], row["name"])
    return names


def humanize(part_id):
    """Tidy a raw descriptor id into a readable label: _SUBWINGS_A3 -> 'Subwings A3',
    _WINGS_NONE -> 'Wings (none)'. The curated names in names/<ship>.json override this."""
    tokens = part_id.strip("_").split("_")
    if not tokens or not tokens[0]:
        return part_id
    group, rest = tokens[0].title(), "_".join(tokens[1:])
    if not rest:
        return group
    up = rest.upper()
    if up.startswith(("NULL", "NONE")):
        return f"{group} (none)"
    flag = ""
    if up.endswith("XNEVER"):
        rest, flag = rest[:-6], " (unused)"
    elif up.endswith("XRARE"):
        rest, flag = rest[:-5], " (rare)"
    return f"{group} {rest.title()}{flag}".strip()


def part_label(names, part_id):
    """Curated name if we have one, otherwise a tidied version of the raw id."""
    return names.get(part_id) or humanize(part_id)


def ship_alias(filename):
    base = os.path.basename(filename or "").lower().replace(".scene.mbin", ".descriptor.mbin")
    return next((alias for alias, rel in ROOTS.items() if rel.split("/")[-1] == base), None)


class GameRunning(RuntimeError):
    pass


def _running_under_proton():
    """Linux/Steam Deck: the game runs under Proton as a process named NMS.exe."""
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
                    if f.read().strip().lower() == "nms.exe":
                        return True
            except OSError:
                continue
    return False


def game_running():
    """True / False, or None when it can't be told (then saving is refused, to be safe)."""
    if sys.platform != "win32":
        return _running_under_proton() if os.path.isdir("/proc") else None
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq NMS.exe"], capture_output=True, text=True, timeout=5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "NMS.exe" in out


def file_stamp(path):
    st = os.stat(path)
    return st.st_mtime_ns, st.st_size


def open_save(path):
    """The save as the player is editing it (its draft); see webapp/drafts.py."""
    from webapp import drafts
    return drafts.open_save(path)


def write(opened):
    """Put an edit into the save's draft. Nothing reaches the file until the player saves; returns None."""
    from webapp import drafts
    return drafts.write(opened)


GAME_RUNNING_MSG = "No Man's Sky is running -- quit the game first, or it will overwrite this change."
GAME_UNKNOWN_MSG = ("Could not check whether No Man's Sky is running, so nothing was written. "
                    "Make sure the game is closed and try again.")

_RUNNING = {"at": 0.0, "value": False}


def ensure_game_closed(msg=GAME_RUNNING_MSG):
    """Raise GameRunning unless the game is known to be closed."""
    running = game_running()
    if running is None:
        raise GameRunning(GAME_UNKNOWN_MSG)
    if running:
        raise GameRunning(msg)


def game_running_cached(max_age=4.0):
    """game_running() for status polls: shared and refreshed at most every few seconds. Unknown counts as running."""
    now = time.time()
    if now - _RUNNING["at"] > max_age:
        _RUNNING["value"], _RUNNING["at"] = game_running() is not False, now
    return _RUNNING["value"]


# Backup restores replace the whole file, so they are staged and confirmed on their own, by token.
PENDING: dict[str, dict] = {}
PENDING_KEEP, PENDING_SECONDS = 2, 600
PENDING_LOCK = threading.Lock()


def stage(path, after, commit):
    """Hold a change until the player confirms it. `commit(stamp)` does the write (refusing if the file
    is no longer the one summarised) and returns extra response fields. Returns {token, file, changes,
    total}, the summary the page shows."""
    from webapp import summary
    stamp = file_stamp(path)
    changes, total = summary.describe(save.open_save(path).readable, after)
    now = time.time()
    token = uuid.uuid4().hex
    with PENDING_LOCK:
        for t in [t for t, p in PENDING.items() if now - p["made"] > PENDING_SECONDS]:
            del PENDING[t]
        while len(PENDING) >= PENDING_KEEP:
            del PENDING[next(iter(PENDING))]
        PENDING[token] = {"path": path, "stamp": stamp, "commit": commit, "response": None, "made": now}
    info = {"token": token, "file": os.path.basename(path), "changes": changes, "total": total}
    if has_request_context():
        g.save_preview = info
    return info


def commit(token):
    """Do a staged change exactly as it was shown. A refusal the player can fix (game running, unsaved
    edits) keeps the token, so they can fix it and confirm again."""
    from webapp import drafts
    with PENDING_LOCK:
        p = PENDING.pop(token, None)
    if not p:
        raise ValueError("Nothing was written: this summary expired. Do it again to see a fresh one.")
    try:
        return {**(p["response"] or {"ok": True}), **p["commit"](p["stamp"])}
    except (GameRunning, drafts.UnsavedEdits, drafts.SaveLocked):
        with PENDING_LOCK:
            PENDING[token] = p
        raise


def attach_preview(resp):
    """after_request: keep the route's normal answer for commit() and add the summary to it."""
    info = g.pop("save_preview", None) if has_request_context() else None
    if info and resp.status_code == 200 and resp.is_json:
        data = {k: v for k, v in (resp.get_json() or {}).items() if k != "preview"}
        with PENDING_LOCK:
            if info["token"] in PENDING:
                PENDING[info["token"]]["response"] = data
        resp.set_data(json.dumps({**data, "preview": info}))
    return resp


def body():
    return request.get_json(force=True) or {}


def limit_seconds(b, default=120):
    """Seconds a search may run; None (or 0) means "keep going until I press Stop"."""
    v = b.get("seconds", default)
    if v in (None, 0, "", "0"):
        return None
    return max(5.0, min(float(v), 86400.0))


GAME_DATA_ERRORS = (gameversion.GameDataError, gamefiles.GameFileError, gamepath.GamePathError,
                    items.ItemError, quests.QuestDataError)


UPDATE_HINT = ("No Man's Sky looks different than expected -- it was probably updated. "
               "Open Save > Game data and press 'Re-read game files'. If that does not help, this part of "
               "the tool needs updating for the new game version.")


def model_filename(ship):
    if ship in SPECIAL_SHIPS:
        return SPECIAL_SHIPS[ship]["entry"]["Resource"]["Filename"]
    if ship not in SHIP_MODELS:
        raise ValueError(f"unknown ship type {ship!r}")
    return SHIP_MODELS[ship]


NO_SEED_LOOK = {"sentinel"}  # sentinel colour comes from its parts and the player's paint, not the seed


def parse_look(raw):
    look = {}
    for k in ("primary", "secondary", "undercoat"):
        v = [int(x) for x in (raw or {}).get(k) or []]
        if any(not 0 <= x < 64 for x in v):
            raise ValueError(f"{k} colour index must be 0-63")
        if v:
            look[k] = set(v)
    modes = [str(m).upper() for m in (raw or {}).get("mode") or []]
    if set(modes) - set(textures.mode_names()):
        raise ValueError(f"unknown hull finish; choose from {', '.join(textures.mode_names())}")
    if modes:
        look["mode"] = set(modes)
    return look

JOBS: dict[str, dict] = {}
