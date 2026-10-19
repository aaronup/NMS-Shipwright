# NMS Shipwright

Windows desktop app (Flask backend + plain-JS page in a pywebview window) for designing, finding and
editing No Man's Sky starships, plus a general save editor. It reproduces the game's own ship
generator, partly reimplemented in Python and partly by running real `NMS.exe` code in an emulator,
so a seed here builds exactly the ship the game builds.

## Commands

Windows, Python 3.11+, and No Man's Sky installed (most features and some tests read the installed
game). Run these from a virtualenv in the repo root.

```
pip install -r requirements.txt pyinstaller

python webapp/app.py                 # dev: UI in a browser at http://127.0.0.1:5000
python nms_editor.py                 # the desktop window (free port, pywebview)

python -m unittest tests.test_robustness                                                # all tests
python -m unittest tests.test_robustness.Designs.test_bad_design_files_are_refused      # one test

python scripts/build_app.py --no-installer   # dist/NMS Shipwright/NMS Shipwright.exe
python scripts/build_app.py                  # + dist/NMS Shipwright Setup.exe (needs Inno Setup 6)
python scripts/build_app.py --console        # debug build with a console (dist-debug/)
```

- No linter or formatter is configured.
- `node --check webapp/static/js/<file>.js` is a quick syntax check for the frontend; there is no JS build step.
- The dev server serves `webapp/static` live from disk with no cache headers: hard-refresh (Ctrl+F5)
  after editing JS/CSS. `dist/` is a snapshot, so rebuild after changes you want in the .exe.
- Releases: `VERSION` lives in `webapp/version.py` (the build script imports it, `/api/version` serves
  it to the page, and `build_appimage.sh` reads it). `.github/workflows/release.yml` builds on a `v*` tag that must match it, using the top
  `CHANGELOG.md` section as the notes.

## Never damage a real save

Saves are the user's real game progress (`%APPDATA%\HelloGames\NMS\st_*\*.hg`).
- When testing in the running app, only use read-only actions (search, preview, load a design). Don't
  trigger writes ("Put in my save...", "Put this design in my save", recolour, inventory edits, import)
  unless the user asks.
- All writes must go through `webapp.common.write(opened)` → `nms_save.save.OpenSave.write()`. That
  refuses while `NMS.exe` is running (`GameRunning` → HTTP 409) and makes a timestamped `.bak` first.
  Don't add a write path that skips it.
- Tests isolate caches via `NMS_TOOL_CACHE` set to a temp dir; follow the `Base` pattern in
  `tests/test_robustness.py`. It also purges `nms_save`/`nms_procgen`/`webapp` from `sys.modules`,
  because module-level paths are read at import.

## Architecture

**Process.** `nms_editor.py` sets `NMS_TOOL_CACHE` (`%LOCALAPPDATA%\NMS Shipwright`), runs
`webapp.app` on a free `127.0.0.1` port in a thread with a 16 MB stack (the emulator needs it), and
shows it in pywebview. `FileBridge` exposes native file dialogs to JS. `webapp/app.py` registers one
blueprint per UI area from `webapp/routes/`. Its error handler maps exceptions to JSON:
`ValueError`/`ShipLookupError` → 400, `GameRunning` → 409, and game-data or game-update errors → 503
with a `hint`. The JS `api()` helper shows that hint. Routes signal user errors by raising `ValueError`
with a readable message.

**Long jobs.** Seed searches, Find-in-game scans and design installs run in a `threading.Thread` and
record progress in `common.JOBS[job_id]` (`status`, `results`, `stats`, `cancel`, `phase`). The page
polls `/api/jobs/<id>` and cancels through `/api/jobs/<id>/cancel`.

**`nms_save/`: save files.** Pipeline: `codec` (.hg LZ4 block decode/encode) → `mapping`
(deobfuscate JSON keys with MBINCompiler's `data/mapping.json`) → `save.OpenSave` (`.readable` is the
deobfuscated tree; `write()` re-obfuscates, encodes and backs up). `ships`, `items`, `extras`, `quests`
and `player` edit that tree. Game location: `gamepath` (`NMS_DIR` env, then Steam libraries including
extra drives, GOG, MS Store). Game files: `gamefiles` copies what it needs out of the PCBANKS `.pak`
archives (via hgpaktool) into the cache. `gameversion` tracks the game build and throws away derived
caches when the game updates. `gamemeta` reads class layouts from `NMS.exe` reflection data.

**`nms_procgen/`: the game's generators.** No game data is shipped. Everything is read from the
player's install at runtime.
- `generator.py`: exact Python reimplementation of procedural part selection (MWC RNG, weighted picks,
  child seeds via `mix64`) over `*.DESCRIPTOR.MBIN` trees (`mbin.py`). `ROOTS` maps ship type → root
  descriptor. `explain()`, `part_ids()` and `generate()` are the main entry points.
- `fastsearch.py`: numpy-vectorised copy of the same RNG that tests about 1M seeds per batch, group by
  group. Every hit is re-confirmed with the exact generator (`matches()`), so "Find seeds" results
  always contain all wanted parts.
- `emu.py` + `exeaddr.py`: map `NMS.exe` into a Unicorn emulator and call real game functions,
  located by byte signatures (`exeaddr.SIGS`, cached in `gamecache/exeaddr.json`). Used by
  `systemgen.py` (which ships a star system has, and dissonance) and `textures.py` (seed → palette
  colours and hull texture modes).
- `locate.py` runs the RNG backwards (seed → which system it came from). `model3d.py` builds 3D models
  from the scene and mesh files and packs a binary that `webapp/static/viewer.js` draws with WebGL2.
- Data shipped in the repo: `names/<ship>.json` (curated community part names), `designs/` (built-in
  saved designs; user designs go to `<cache>/designs`), `test_vectors.json` (known seed → parts,
  checked by `GeneratorVectors`).

**Frontend (`webapp/static/`).** `editor.html` plus classic `<script>` files, no modules and no
bundler. They share globals from `core.js` (`$`, `$$`, `S` state, `api`, `post`, `guard`, `toast`,
`show3d`, `modelUrl`), and functions are called across files (e.g. `designer.js` calls `applySeed` from
`ships.js`, `find.js` reuses `shipCard`/`previewBtn`). Load order in `editor.html` matters.

## Rules for the generator code

The comments in `nms_procgen/` are load-bearing warnings:
- `generator.py`, `fastsearch.py` and `textures.py` must match the game bit for bit. Don't "clean up"
  the maths, change integer widths, or switch float32 to float64. `fastsearch.py` must change in
  lockstep with `generator.py`.
- If an `exeaddr` signature stops matching, the game was patched: redump the bytes, don't edit the
  logic. Struct offsets in `systemgen.py` move with game updates.
- `build_app.py:fix_pe` (16 MB stack, Control Flow Guard stripped) is required or the packaged app dies
  silently. Expect unsigned PyInstaller builds to trip antivirus heuristics (false positives).

## Style

Match the surrounding code: terse comments explaining *why*, short helpers, plain-English UI and error
text aimed at players. `nms_procgen` comments use a clipped shorthand ("dont", "nvr", "w/"), so keep it
there. Python uses `from __future__ import annotations` and module docstrings.
