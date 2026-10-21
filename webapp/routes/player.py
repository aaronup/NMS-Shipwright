"""Currencies, quests, backups and whole-save export/import."""
from __future__ import annotations

import glob
import os
import re

from flask import Blueprint, jsonify, request

from nms_save import player, quests, save

from webapp import drafts
from webapp.common import PENDING, PENDING_LOCK, body, commit, ensure_game_closed, open_save, stage, write

bp = Blueprint("player", __name__)


@bp.route("/api/player", methods=["GET", "POST"])
def api_player():
    if request.method == "GET":
        return jsonify(player.get_currencies(open_save(request.args.get("path")).readable))
    b = body()
    opened = open_save(b.get("path"))
    values = player.set_currencies(opened.readable, **{k: b[k] for k in player.CURRENCIES if k in b})
    return jsonify({"ok": True, "values": values, "backup": write(opened)})


@bp.route("/api/quests")
def api_quests():
    return jsonify(quests.list_quests(open_save(request.args.get("path")).readable))


@bp.route("/api/quests/skip", methods=["POST"])
def api_quests_skip():
    b = body()
    opened = open_save(b.get("path"))
    changed = quests.skip_quests(opened.readable, list(b.get("keys", [])))
    if not changed:
        return jsonify({"ok": True, "changed": [], "backup": None})
    return jsonify({"ok": True, "changed": changed, "backup": write(opened)})


BACKUP_TIME = re.compile(r"\.(\d{8})-(\d{6})-\d+\.bak$")


@bp.route("/api/backups")
def api_backups():
    path = request.args.get("path")
    drafts.check_path(path)
    out = []
    for f in glob.glob(f"{path}.*.bak"):
        m = BACKUP_TIME.search(f)
        day, clock = (m.group(1), m.group(2)) if m else ("", "")
        made = f"{day[:4]}-{day[4:6]}-{day[6:]} {clock[:2]}:{clock[2:4]}:{clock[4:]}" if m else ""
        out.append({"file": f, "made": made, "size": os.path.getsize(f)})
    return jsonify(sorted(out, key=lambda x: x["made"], reverse=True))


@bp.route("/api/backups/restore", methods=["POST"])
def api_backups_restore():
    b = body()
    path, src = b.get("path"), b.get("file")
    drafts.check_path(path)
    ensure_game_closed("No Man's Sky is running -- quit the game first.")
    data, restore = drafts.restore(path, src)  # refuses unknown files and unsaved edits
    # Always only staged: nothing replaces the save until the player confirms the summary (/api/save/commit).
    stage(path, save.from_bytes(src, data).readable, restore)
    return jsonify({"ok": True, "safety_copy": None})


@bp.route("/api/draft")
def api_draft():
    """Unsaved edits to this save, and whether the file changed under them. Cheap: no decoding.
    Also marks this save as the one the page has open (it holds the save's lock)."""
    return jsonify(drafts.state(request.args.get("path")))


@bp.route("/api/drafts")
def api_drafts():
    """Every save with unsaved edits, including ones left from a previous session."""
    return jsonify(drafts.listing())


@bp.route("/api/draft/review", methods=["POST"])
def api_draft_review():
    """Every unsaved edit, compared with the file on disk."""
    return jsonify(drafts.review(body().get("path")))


@bp.route("/api/draft/save", methods=["POST"])
def api_draft_save():
    b = body()
    return jsonify(drafts.save_draft(b.get("path"), b.get("rev")))


@bp.route("/api/draft/discard", methods=["POST"])
def api_draft_discard():
    """Throw unsaved edits away; with "rev", only if they are still the ones the page showed."""
    b = body()
    drafts.discard(b.get("path"), b.get("rev"))
    return jsonify({"ok": True})


@bp.route("/api/save/commit", methods=["POST"])
def api_save_commit():
    """Do a backup restore the player has just seen the summary of."""
    return jsonify(commit(body().get("token")))


@bp.route("/api/save/discard", methods=["POST"])
def api_save_discard():
    with PENDING_LOCK:
        PENDING.pop(body().get("token"), None)
    return jsonify({"ok": True})
