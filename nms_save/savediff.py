"""What a change to a save tree actually changes: a list of leaf-level differences.

Paths are lists of dict keys, list indexes and ("at", x, y) for inventory slots, which are
matched by their grid position rather than their place in the list. Big batches of list
additions or removals (thousands of learned words, say) come back as one entry with a count.
"""
from __future__ import annotations

import json
from collections import Counter

BATCH = 10  # more added/removed list items than this are reported as one counted entry


def _key(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"))


def _slot_key(item):
    i = item.get("Index") if isinstance(item, dict) else None
    if isinstance(i, dict) and "X" in i and "Y" in i:
        return ("at", i["X"], i["Y"])
    return None


def diff(before, after, limit: int = 300) -> tuple[list[dict], int]:
    """Differences from `before` to `after`: (the first `limit` entries, how many there are in all).

    Each entry is {"path", "kind": "changed" | "added" | "removed", "before", "after"}; a batch
    entry also has "count" and keeps a few of the items in "sample". "path" indexes the `after`
    tree (the `before` one for removals); when a list item moved, "bpath" is where it was in `before`.

    Values must be equal *and* the same type to count as unchanged: true -> 1 changes the file.
    """
    out: list[dict] = []
    total = 0

    def add(path, kind, b=None, a=None, bpath=None, **extra):
        nonlocal total
        total += 1
        if len(out) < limit:
            row = {"path": path, "kind": kind, "before": b, "after": a, **extra}
            if bpath is not None and bpath != path:
                row["bpath"] = bpath
            out.append(row)

    def items(path, bpath, kind, values):
        if len(values) > BATCH:
            add(path if kind == "added" else bpath, kind, count=len(values), sample=[v for _, v in values[:3]])
        else:
            for seg, v in values:
                if kind == "removed":
                    add(bpath + [seg], kind, v)
                else:
                    add(path + [seg], kind, None, v)

    def same(b, a):
        if type(b) is not type(a):
            return False
        if isinstance(b, (dict, list)):
            # == is fast but says true == 1; the exact-text check runs only on subtrees that look equal
            return b == a and _key(b) == _key(a)
        return b == a or (b != b and a != a)  # NaN

    def walk(b, a, path, bpath):
        if same(b, a):
            return
        if isinstance(b, dict) and isinstance(a, dict):
            for k, v in b.items():
                if k in a:
                    walk(v, a[k], path + [k], bpath + [k])
                else:
                    add(bpath + [k], "removed", v)
            for k, v in a.items():
                if k not in b:
                    add(path + [k], "added", None, v)
        elif isinstance(b, list) and isinstance(a, list):
            walk_list(b, a, path, bpath)
        else:
            add(path, "changed", b, a, bpath)

    def walk_list(b, a, path, bpath):
        bk, ak = [_slot_key(x) for x in b], [_slot_key(x) for x in a]
        if b and a and None not in bk and None not in ak and len(set(bk)) == len(bk) and len(set(ak)) == len(ak):
            bm, am = dict(zip(bk, b)), dict(zip(ak, a))
            for k in bk:
                if k in am:
                    walk(bm[k], am[k], path + [k], bpath + [k])
            items(path, bpath, "removed", [(k, bm[k]) for k in bk if k not in am])
            items(path, bpath, "added", [(k, am[k]) for k in ak if k not in bm])
            return
        if len(b) == len(a):
            for i, (x, y) in enumerate(zip(b, a)):
                walk(x, y, path + [i], bpath + [i])
            return
        # Lengths differ: items present on both sides are unchanged, wherever they moved to.
        left = Counter(_key(x) for x in a)
        gone = []
        for i, x in enumerate(b):
            k = _key(x)
            if left[k]:
                left[k] -= 1
            else:
                gone.append((i, x))
        right = Counter(_key(x) for x in b)
        new = []
        for i, x in enumerate(a):
            k = _key(x)
            if right[k]:
                right[k] -= 1
            else:
                new.append((i, x))
        # An edited item shows up as one gone and one new; pair those up when they are the same kind of thing.
        scalar = lambda v: not isinstance(v, (dict, list))
        while gone and new and not scalar(gone[0][1]) and type(gone[0][1]) is type(new[0][1]):
            (i, x), (j, y) = gone.pop(0), new.pop(0)
            walk(x, y, path + [j], bpath + [i])
        items(path, bpath, "removed", gone)
        items(path, bpath, "added", new)

    walk(before, after, [], [])
    return out, total
