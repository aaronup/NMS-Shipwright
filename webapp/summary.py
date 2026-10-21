"""Turn a save diff into lines a player can read: "Ship slot 3 (Iron Vulture) › Name: 'A' → 'B'"."""
from __future__ import annotations

import re

from nms_save import extras, items, savediff

SKIP = {"BaseContext", "PlayerStateData", "CommonStateData", "Resource"}

AREAS = {"ShipOwnership": "Ship slot", "Multitools": "Multi-tool", "FleetFrigates": "Frigate", "Pets": "Companion",
         "VehicleOwnership": "Exocraft"}

NAMES = {"Specials": "Quicksilver", "Inventory": "Inventory", "Inventory_TechOnly": "Technology",
         "Inventory_Cargo": "Cargo", "KnownTech": "Known blueprints", "KnownProducts": "Known recipes",
         "KnownRefinerRecipes": "Known refiner recipes", "KnownWordGroups": "Known words",
         "KnownSpecials": "Known specials", "Filename": "Ship type", "Seed": "Seed", "Slots": "Slots",
         "ValidSlotIndices": "Usable slots", "BaseStatValues": "Stats", "Store": "Inventory"}


def _words(key: str) -> str:
    return NAMES.get(key) or re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key.replace("_", " ")).strip()


def _top(key: str) -> str:
    """A player-level key, naming its inventories the way the Inventory page does ("Exosuit cargo")."""
    m = re.fullmatch(r"Chest(\d+)Inventory", key)
    if m:
        return f"Storage container {int(m.group(1)) - 1}"
    return items.FRIENDLY.get(key) or _words(key)


def _catalogue_name(bare: str):
    try:
        return items.catalogue().get(bare, {}).get("name")
    except Exception:  # game data unreadable: the id is still a fine label
        return None


def _item_name(item_id: str) -> str:
    bare = item_id.lstrip("^")
    name = _catalogue_name(bare)
    return f"{name} ({bare})" if name and name != bare else bare


def _plain_name(item_id: str) -> str:
    """Just the item's name, for naming a slot ("slot 7,6 (Carbon)")."""
    bare = str(item_id).lstrip("^")
    return _catalogue_name(bare) or bare


def _word(group: str) -> str:
    """A learned alien word: "Atlas word 'please'" from ^ATLAS_PLEASE."""
    bare = group.lstrip("^")
    try:
        race = extras.RACES.get(extras.word_groups().get(bare))
    except Exception:
        race = None
    head, _, rest = bare.partition("_")
    if race is None and head.title() in {r.replace("'", "").title() for r in extras.RACES.values()}:
        race = head.title()
    word = (rest if race and head.upper() == race.replace("'", "").upper() else bare).replace("_", " ").lower()
    return f"{race} word '{word}'" if race else f"word '{word}'"


def _recipe(recipe_id: str) -> str:
    bare = recipe_id.lstrip("^")
    m = re.fullmatch(r"(REFINERECIPE|COOK)_?(\d+)", bare, re.IGNORECASE)
    if m:
        return f"{'Refiner' if m.group(1).upper().startswith('REFINE') else 'Cooking'} recipe {int(m.group(2))}"
    return bare


def _ship_type(filename) -> str:
    from webapp.common import SHIP_LABELS, ship_alias
    alias = ship_alias(filename) if isinstance(filename, str) else None
    return SHIP_LABELS.get(alias) or (show(filename) if filename else "empty")


# how to name the items of a few lists that hold bare ids
LIST_ITEMS = {"KnownWordGroups": lambda v: _word(v["Group"]) if isinstance(v, dict) and "Group" in v else show(v),
              "KnownRefinerRecipes": lambda v: _recipe(v) if isinstance(v, str) else show(v)}


def show_in(parent, v) -> str:
    """show(), knowing which list the value sits in."""
    return LIST_ITEMS[parent](v) if parent in LIST_ITEMS else show(v)


def show(v) -> str:
    """A short, readable version of one value."""
    if v is None:
        return "nothing"
    if isinstance(v, str):
        if v.startswith("^") and len(v) > 1:
            return _item_name(v)
        if re.fullmatch(r"0x[0-9A-Fa-f]+", v):  # a seed
            return v
        if v.upper().startswith("MODELS/"):
            return v.split("/")[-1].split(".")[0].replace("_", " ").title()
        if not v:
            return "(blank)"
        return repr(v if len(v) <= 80 else v[:77] + "...")
    if isinstance(v, dict):
        if "Id" in v:
            amount = v.get("Amount")
            return _item_name(str(v["Id"])) + (f" ×{amount}" if isinstance(amount, int) and amount > 1 else "")
        if "Group" in v:
            return show(v["Group"])
        if isinstance(v.get("Name"), str) and v["Name"]:
            return repr(v["Name"])
        return f"{{{len(v)} fields}}"
    if isinstance(v, list):
        if len(v) == 2 and isinstance(v[0], bool):  # seeds are stored as [has_seed, "0x..."]
            return str(v[1])
        return f"[{len(v)} items]"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _label(path: list, tree) -> tuple[str, str]:
    """(area, rest) for a diff path, naming list entries after what they hold."""
    parts, node = [], tree
    i = 0
    while i < len(path):
        seg = path[i]
        nxt = node[seg] if isinstance(node, dict) and seg in node else None
        if isinstance(seg, str) and seg in AREAS and i + 1 < len(path) and isinstance(path[i + 1], int):
            idx = path[i + 1]
            entry = (nxt or [])[idx] if isinstance(nxt, list) and idx < len(nxt) else None
            name = entry.get("Name") if isinstance(entry, dict) else None
            if not name and seg == "ShipOwnership" and isinstance(entry, dict):
                name = _ship_type((entry.get("Resource") or {}).get("Filename"))
            parts.append(f"{AREAS[seg]} {idx}" + (f" ({name})" if name else ""))
            node, i = entry, i + 2
            continue
        if isinstance(seg, tuple):
            item = next((x for x in node if isinstance(x, dict) and x.get("Index") == {"X": seg[1], "Y": seg[2]}),
                        None) if isinstance(node, list) else None
            name = _plain_name(item["Id"]) if item and item.get("Id") else None
            parts.append(f"slot {seg[1]},{seg[2]}" + (f" ({name})" if name else ""))
            node = item
        elif isinstance(seg, int):
            if not (isinstance(node, list) and parts and parts[-1] == "Seed"):
                parts.append(f"#{seg}")
            node = node[seg] if isinstance(node, list) and seg < len(node) else None
        else:
            slot_next = i + 1 < len(path) and isinstance(path[i + 1], tuple)
            if seg in SKIP or (seg == "Slots" and slot_next):  # "Exosuit › slot 7,6 (Acid)" says enough
                pass
            elif not parts:
                parts.append(_top(seg))
            else:
                parts.append(_words(seg))
            node = nxt
        i += 1
    if not parts:
        return "Whole save", ""
    return parts[0], " › ".join(parts[1:])


def describe(before, after, limit: int = 300) -> tuple[list[dict], int]:
    """Readable changes from `before` to `after`, and how many changes there are in all."""
    raw, total = savediff.diff(before, after, limit)
    out = []
    for d in raw:
        path = d["path"]
        parent = next((p for p in reversed(path) if isinstance(p, str)), None)
        # name things by the tree that has them: the new one for additions, the old one otherwise
        if d["kind"] == "added":
            area, what = _label(d["path"], after)
        else:
            area, what = _label(d.get("bpath", d["path"]), before)
        row = {"area": area, "what": what, "kind": d["kind"]}
        if "count" in d:
            row["count"] = d["count"]
            row["sample"] = [show_in(parent, v) for v in d["sample"]]
        else:
            if d["kind"] != "added":
                row["before"] = show_in(parent, d["before"])
            if d["kind"] != "removed":
                row["after"] = show_in(parent, d["after"])
        out.append(row)
    return out, total
