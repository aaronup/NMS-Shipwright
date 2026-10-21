"""NMS Shipwright -- the local backend behind the desktop window (nms_editor.py).

Binds to 127.0.0.1 only. Edits never write the save file: they go into a draft of it
(webapp/drafts.py), kept in the tool's cache so unsaved edits survive a restart, and locked so
only one Shipwright window edits a save at a time. The player reviews a summary of the draft and
saves it (/api/draft/save): refused while No Man's Sky is running or if the file changed on disk,
backed up first, written atomically and read back. Restoring a backup replaces the file directly,
after its own preview (/api/save/commit). Requests that change anything must come from this app's
own page (same origin), so another web page can't reach the API.

    python nms_editor.py          # desktop window
    python webapp/app.py          # the same UI in a browser at http://127.0.0.1:5000

The routes live in webapp/routes/, one module per part of the UI.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from urllib.parse import urlsplit  # noqa: E402

from flask import Flask, jsonify, request  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402

from nms_save import ships  # noqa: E402
from webapp.common import GAME_DATA_ERRORS, GameRunning, UPDATE_HINT, attach_preview  # noqa: E402
from webapp.drafts import ReadBackError, SaveLocked, release_edit_locks  # noqa: E402
from webapp.routes import designer, find, health, inventory, pages, player, ships as ship_routes  # noqa: E402

app = Flask(__name__, static_folder=None)
for module in (pages, health, ship_routes, designer, find, inventory, player):
    app.register_blueprint(module.bp)
app.after_request(attach_preview)
app.teardown_request(release_edit_locks)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@app.before_request
def only_this_app():
    """Only this app's own page may use the API. Another web page open in a browser could otherwise
    send requests here (a cross-site form or fetch), or reach it through a DNS name pointed at
    127.0.0.1, and change a save."""
    if (urlsplit("//" + (request.host or "")).hostname or "") not in LOCAL_HOSTS:
        return jsonify({"ok": False, "error": "Shipwright only answers its own window."}), 403
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if source:
        if source == "null" or urlsplit(source).netloc.lower() != (request.host or "").lower():
            return jsonify({"ok": False, "error": "Shipwright only accepts changes from its own window."}), 403
    elif request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        return jsonify({"ok": False, "error": "Shipwright only accepts changes from its own window."}), 403
    return None


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):  # 404s etc. keep their own status
        return e
    from nms_procgen.exeaddr import AddressError
    from nms_save.gamemeta import MetaError
    if isinstance(e, (MetaError, AddressError) + GAME_DATA_ERRORS):
        return jsonify({"ok": False, "error": str(e), "hint": UPDATE_HINT, "game_data": True}), 503
    if isinstance(e, SaveLocked):
        return jsonify({"ok": False, "error": str(e), "locked": True}), 423
    if isinstance(e, ReadBackError):
        return jsonify({"ok": False, "error": str(e), "backup": e.backup}), 500
    code = 409 if isinstance(e, GameRunning) else 400 if isinstance(e, (ValueError, ships.ShipLookupError)) else 500
    return jsonify({"ok": False, "error": str(e)}), code


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
