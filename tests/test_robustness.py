"""Game-update robustness: caches tied to the game build, extraction stamps, graceful failures.

    python tests/test_robustness.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        os.environ["NMS_TOOL_CACHE"] = self.dir
        for mod in [m for m in list(sys.modules) if m.startswith(("nms_save", "nms_procgen", "webapp"))]:
            del sys.modules[mod]
        from nms_save import gamefiles, gameversion
        self.gamefiles, self.gameversion = gamefiles, gameversion
        gamefiles.CACHE = self.dir
        gameversion.BUILD_FILE = os.path.join(self.dir, "build.json")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("NMS_TOOL_CACHE", None)
        sys.modules.pop("hgpaktool", None)  # tests that fake it must not leave it behind


class BuildTracking(Base):
    def test_new_build_drops_caches_derived_from_the_old_one(self):
        gv = self.gameversion
        os.makedirs(os.path.join(self.dir, "models"))
        with open(os.path.join(self.dir, "models", "a.npz"), "wb") as f:
            f.write(b"x")
        gv.build_id = lambda: {"exe": [1, 1], "paks": 1}
        self.assertTrue(gv.check()["first_run"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "models", "a.npz")), "same build keeps the cache")

        gv.check.cache_clear()
        gv.build_id = lambda: {"exe": [2, 2], "paks": 1}
        state = gv.check()
        self.assertTrue(state["changed"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "models")), "a game update drops derived caches")

    def test_missing_game_is_reported_clearly(self):
        gv = self.gameversion
        gv.exe_path = lambda: os.path.join(self.dir, "nope", "NMS.exe")
        with self.assertRaises(gv.GameDataError) as err:
            gv.build_id()
        self.assertIn("Game data", str(err.exception))


class Extraction(Base):
    def _fake_pak(self):
        calls = []

        class FakePak:
            def __init__(self, path):
                self.path = path

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract(self, filters):
                calls.append(tuple(filters))
                for f in filters:
                    yield f.replace("*", ""), b"data"

        sys.modules["hgpaktool"] = type("m", (), {"HGPAKFile": FakePak})
        with open(os.path.join(self.dir, "NMSARC.Test.pak"), "wb") as f:
            f.write(b"pak")
        self.gamefiles.banks = lambda: self.dir
        return calls

    def test_two_callers_can_share_a_folder(self):
        calls = self._fake_pak()
        dest = os.path.join(self.dir, "tables")
        self.gamefiles.extract(dest, ["one.mbin*"], "NMSARC.Test.pak")
        self.gamefiles.extract(dest, ["two.mbin*"], "NMSARC.Test.pak")
        self.assertTrue(os.path.exists(os.path.join(dest, "one.mbin")), "the second caller kept the first one's file")
        self.assertTrue(os.path.exists(os.path.join(dest, "two.mbin")))
        self.gamefiles.extract(dest, ["one.mbin*"], "NMSARC.Test.pak")
        self.assertEqual(len(calls), 2, "an unchanged game is not read again")

    def test_a_deleted_copy_is_extracted_again_next_run(self):
        """Within one run the check is remembered; a restart repairs a cache someone deleted from."""
        calls = self._fake_pak()
        dest = os.path.join(self.dir, "tables")
        self.gamefiles.extract(dest, ["one.mbin*"], "NMSARC.Test.pak")
        os.remove(os.path.join(dest, "one.mbin"))
        self.gamefiles.extract(dest, ["one.mbin*"], "NMSARC.Test.pak")
        self.assertEqual(len(calls), 1, "repeat calls in one run do not re-check every file")
        self.gamefiles._VERIFIED.clear()  # as if the app were restarted
        self.gamefiles.extract(dest, ["one.mbin*"], "NMSARC.Test.pak")
        self.assertEqual(len(calls), 2)
        self.assertTrue(os.path.exists(os.path.join(dest, "one.mbin")))

    def test_nothing_found_says_what_to_do(self):
        class Empty:
            def __init__(self, path):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract(self, filters):
                return iter(())

        self._fake_pak()
        sys.modules["hgpaktool"] = type("m", (), {"HGPAKFile": Empty})
        with self.assertRaises(self.gamefiles.GameFileError) as err:
            self.gamefiles.extract(os.path.join(self.dir, "t"), ["missing.mbin*"], "NMSARC.Test.pak")
        self.assertIn("Re-read game files", str(err.exception))


class GracefulFailure(Base):
    def test_unknown_item_falls_back_to_its_id(self):
        from nms_save import items
        it = items.item("^NOT_A_REAL_ITEM")
        self.assertEqual(it["id"], "NOT_A_REAL_ITEM")
        self.assertEqual(it["name"], "Not A Real Item")

    def test_item_names_survive_unreadable_game_tables(self):
        from nms_save import items
        items.catalogue.cache_clear()
        broken = lambda: (_ for _ in ()).throw(items.ItemError("tables unreadable"))
        items.catalogue, real = broken, items.catalogue
        try:
            self.assertEqual(items.item("^FUEL1")["name"], "Fuel1")
        finally:
            items.catalogue = real

    def test_unreadable_exe_names_the_cause(self):
        from nms_save import gamemeta
        path = os.path.join(self.dir, "NMS.exe")
        with open(path, "wb") as f:
            f.write(b"not a program")
        with self.assertRaises(gamemeta.MetaError) as err:
            gamemeta.ExeMeta(path)
        self.assertIn("could not be read", str(err.exception))

    def test_save_with_a_stray_non_utf8_byte_still_decodes(self):
        # a customer's save carried a byte that is not valid UTF-8; strict decode used to crash
        # with "'utf-8' codec can't decode byte 0x80". It must decode and round-trip instead.
        import json
        from nms_save import codec
        raw = b'{"a":"name ab\x80cd","n":5}\x00'
        text, chunked, nul = codec.decode(raw)
        self.assertEqual(json.loads(text)["n"], 5)
        reraw = codec.encode(json.dumps(json.loads(text), separators=(",", ":")), chunked, nul)
        self.assertEqual(json.loads(codec.decode(reraw)[0])["n"], 5)


class ShipModels(unittest.TestCase):
    """The viewer must never end up with an invisible ship."""

    def setUp(self):
        os.environ.pop("NMS_TOOL_CACHE", None)  # these need the real copies of the game files
        for mod in [m for m in list(sys.modules) if m.startswith(("nms_save", "nms_procgen", "webapp"))]:
            del sys.modules[mod]
        from nms_procgen import model3d
        self.m = model3d
        self.real = model3d._material_class
        try:
            model3d.ship_meshes(0xA547AB958C97E439, "fighter")
        except Exception as e:
            self.skipTest(f"game files not available: {e}")

    def tearDown(self):
        self.m._material_class = self.real

    def _classes(self):
        return {c for _, _, _, c in self.m.ship_meshes(0xA547AB958C97E439, "fighter")}

    def test_unreadable_materials_still_draw_the_hull(self):
        self.m._material_class = lambda material: (None, "")
        self.assertTrue(self._classes() & {"paint", "metal", "secondary"})

    def test_materials_that_all_claim_to_be_invisible_are_not_trusted(self):
        for pretend in (12, 14, 59, 70):  # decal, mask, shadow-only, volume
            self.m._material_class = lambda material, k=pretend: (k, "")
            self.assertTrue(self._classes(), f"class {pretend} hid the whole ship")

    def test_index_buffers_stay_inside_their_mesh(self):
        """The "16 bit indices" flag lies on some ships; a wrong width draws a black mess."""
        for ship, parts, seed in (("sentinel", ["_PIT_A"], 0), ("sentinel", None, 0x1234567890ABCDEF),
                                  ("fighter", None, 0xA547AB958C97E439)):
            model = self.m.ship_model(seed, ship, parts)
            for cls, (pos, idx) in model["groups"].items():
                self.assertTrue(len(idx) % 3 == 0, f"{ship} {cls} has a partial triangle")
                self.assertLess(int(idx.max()), len(pos), f"{ship} {cls} points outside its own vertices")

    def test_a_half_finished_design_shows_a_whole_ship(self):
        """Picking one part must not hide every group the design has not chosen yet."""
        full = len(self.m.ship_meshes(0, "sentinel", []))
        one = self.m.ship_meshes(0, "sentinel", ["_PIT_A"])
        other = self.m.ship_meshes(0, "sentinel", ["_PIT_C"])
        self.assertGreater(len(one), 20, "one pick left almost nothing to draw")
        self.assertGreater(full, 20)
        self.assertNotEqual([m[1] for m in one], [m[1] for m in other], "the pick changed nothing")

    def test_lower_detail_copies_are_left_out(self):
        for name in ("BodyLOD1", "WingsALOD2", "SUB3_Wings_GLOD3"):
            self.assertNotEqual(self.m.LOD_SUFFIX.search(name).group(1), "0")
        self.assertIsNone(self.m.LOD_SUFFIX.search("Body"))
        self.assertEqual(self.m.LOD_SUFFIX.search("WingsALOD0").group(1), "0")


class Designs(unittest.TestCase):
    """Saved designs: files that are checked before use, and seeds that really build the design."""

    def setUp(self):
        os.environ.pop("NMS_TOOL_CACHE", None)
        for mod in [m for m in list(sys.modules) if m.startswith(("nms_save", "nms_procgen", "webapp"))]:
            del sys.modules[mod]
        try:
            from webapp.routes import designer
        except Exception as e:
            self.skipTest(f"app not importable here: {e}")
        self.designs = designer

    def test_bad_design_files_are_refused(self):
        for bad in ({"ship": "banana", "want": ["_PIT_A"]}, {"ship": "sentinel", "want": ["_NOT_A_PART"]},
                    {"ship": "sentinel", "want": []}, ["not", "a", "design"]):
            with self.assertRaises(ValueError):
                self.designs.clean_design(bad)

    def test_a_known_seed_is_only_used_if_it_still_builds_the_design(self):
        d = next(x for x in self.designs.read_designs() if x["id"] == "dark_unicorn")
        self.assertEqual(self.designs.matching_seed(d["ship"], d["want"], d["seeds"]), d["seeds"][0])
        self.assertIsNone(self.designs.matching_seed(d["ship"], d["want"], ["0x1", "0x2"]))


class ShipEquipment(unittest.TestCase):
    """A ship must carry the parts, mark and class of the type it actually is."""

    FIGHTER = "MODELS/COMMON/SPACECRAFT/FIGHTERS/FIGHTER_PROC.SCENE.MBIN"
    SENTINEL = "MODELS/COMMON/SPACECRAFT/SENTINELSHIP/SENTINELSHIP_PROC.SCENE.MBIN"

    def setUp(self):
        for mod in [m for m in list(sys.modules) if m.startswith("nms_save")]:
            del sys.modules[mod]
        from nms_save import ships
        self.ships = ships

    def _save(self, filename, tech):
        def inv(ids):
            return {"Slots": [{"Type": {"InventoryType": "Technology"}, "Id": "^" + i, "Amount": 1, "MaxAmount": 1,
                               "DamageFactor": 0.0, "FullyInstalled": True, "Index": {"X": n, "Y": 0}}
                              for n, i in enumerate(ids)],
                    "ValidSlotIndices": [{"X": x, "Y": y} for y in range(3) for x in range(6)],
                    "Class": {"InventoryClass": "C"},
                    "BaseStatValues": [{"BaseStatID": "^SHIP_DAMAGE", "Value": 1.0}]}
        entry = {"Name": "", "Resource": {"Filename": filename, "Seed": [True, "0x1"]},
                 "Inventory": inv([]), "Inventory_TechOnly": inv(tech), "Inventory_Cargo": inv([])}
        return {"BaseContext": {"PlayerStateData": {"ShipOwnership": [entry]}}}, entry

    def _ids(self, entry):
        return sorted(s["Id"].lstrip("^") for s in entry["Inventory_TechOnly"]["Slots"])

    def _stats(self, entry):
        return [s["BaseStatID"].lstrip("^") for s in entry["Inventory"]["BaseStatValues"]]

    def test_a_sentinel_gets_interceptor_parts_and_the_interceptor_mark(self):
        save, entry = self._save(self.SENTINEL, ["LAUNCHER", "SHIPJUMP1", "SHIPSHIELD", "SHIPGUN1"])
        self.assertTrue(self.ships.wrong_core_tech(entry), "fighter parts on a sentinel are a fault")
        self.ships.set_core_tech(save, 0, True)
        self.assertEqual(self._ids(entry), ["HYPERDRIVE_ROBO", "LAUNCHER_ROBO", "LIFESUP_ROBO",
                                            "SHIPGUN_ROBO", "SHIPJUMP_ROBO", "SHIPSHIELD_ROBO"])
        self.assertIn("ROBOT_SHIP", self._stats(entry), "the game's interceptor mark")
        self.assertEqual(entry["Inventory"]["Class"]["InventoryClass"], "S")
        self.assertEqual(self.ships.wrong_core_tech(entry), [])

    def test_turning_one_back_into_a_starship_undoes_all_of_it(self):
        save, entry = self._save(self.SENTINEL, ["LAUNCHER", "SHIPJUMP1"])
        self.ships.set_core_tech(save, 0, True)
        entry["Resource"]["Filename"] = self.FIGHTER
        self.ships.set_core_tech(save, 0, False)
        self.assertNotIn("LIFESUP_ROBO", self._ids(entry), "no Pilot Interface on an ordinary ship")
        self.assertNotIn("ROBOT_SHIP", self._stats(entry))
        self.assertEqual(self.ships.wrong_core_tech(entry), [])

    def test_stats_stay_inside_what_the_game_rolls(self):
        save, entry = self._save(self.FIGHTER, ["LAUNCHER"])
        try:
            now = self.ships.ship_stats(save, 0)
        except Exception as e:
            self.skipTest(f"game files not available: {e}")
        self.assertTrue(now["stats"], "a fighter has stats")
        first = now["stats"][0]
        with self.assertRaises(ValueError):
            self.ships.set_ship_stats(save, 0, {first["id"]: first["max"] + 50})
        best = self.ships.set_ship_stats(save, 0, ship_class="S", best=True)
        self.assertEqual(best["class"], "S")
        for st in best["stats"]:
            self.assertEqual(st["value"], st["max"], f"{st['name']} should be at its best")

    def test_a_missing_part_is_not_called_a_fault(self):
        """A starter ship with no hyperdrive is the player's business, not a fault to fix."""
        save, entry = self._save(self.FIGHTER, ["LAUNCHER", "SHIPJUMP1"])
        self.assertEqual(self.ships.wrong_core_tech(entry), [])


class GeneratorVectors(unittest.TestCase):
    """The part generator must keep reproducing known ships after a game update."""

    def test_known_ships_still_come_out_the_same(self):
        os.environ.pop("NMS_TOOL_CACHE", None)
        for mod in [m for m in list(sys.modules) if m.startswith(("nms_save", "nms_procgen"))]:
            del sys.modules[mod]
        from nms_procgen.generator import part_ids, stored_id
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "nms_procgen", "test_vectors.json")) as f:
            vectors = json.load(f)
        for t in vectors:
            alias = t.get("aliases", {})          # ids nms.center used before the game renamed them
            want = {stored_id(alias.get(p, p)) for p in t["parts"]}
            try:
                got = set(part_ids(int(str(t["seed"]), 16), t["ship"]))
            except Exception as e:
                self.skipTest(f"game files not available: {e}")
            self.assertLessEqual(want, got, f"{t['seed']} ({t['ship']}) lost {sorted(want - got)}")


class OtherPeoplesPCs(unittest.TestCase):
    """The app has to work on a PC that is not the one it was built on."""

    def setUp(self):
        os.environ.pop("NMS_TOOL_CACHE", None)
        for mod in [m for m in list(sys.modules) if m.startswith(("nms_save", "nms_procgen", "webapp"))]:
            del sys.modules[mod]

    def test_the_game_is_found_without_being_told_where_it_is(self):
        from nms_save import gamepath
        os.environ.pop("NMS_DIR", None)
        gamepath.find.cache_clear()
        folder = gamepath.find()
        if not folder:
            self.skipTest("No Man's Sky is not installed here")
        self.assertTrue(gamepath.looks_like_game(folder))

    def test_a_folder_that_is_not_the_game_is_refused(self):
        from nms_save import gamepath
        self.assertFalse(gamepath.looks_like_game(tempfile.gettempdir()))
        with self.assertRaises(gamepath.GamePathError):
            gamepath.save_dir(tempfile.gettempdir())

    def test_part_data_is_taken_from_the_players_own_game(self):
        """Nothing from the game is shipped, so the parts must come out of their install."""
        cache = tempfile.mkdtemp(prefix="nmstest-")
        os.environ["NMS_TOOL_CACHE"] = cache
        try:
            for mod in [m for m in list(sys.modules) if m.startswith(("nms_save", "nms_procgen"))]:
                del sys.modules[mod]
            from nms_procgen import mbin
            mbin._BUNDLED = os.path.join(cache, "not-bundled")  # as in a build others download
            mbin.gamedata_dir.cache_clear()
            from nms_procgen.generator import all_parts
            try:
                groups = all_parts("sentinel")
            except Exception as e:
                self.skipTest(f"game files not available: {e}")
            self.assertIn("_PIT_", groups)
            self.assertTrue(os.path.isdir(os.path.join(cache, "gamedata", "models")))
        finally:
            os.environ.pop("NMS_TOOL_CACHE", None)
            shutil.rmtree(cache, ignore_errors=True)


class ShipTechIsLearned(Base):
    """Tech the tool puts on a ship must be marked known, or the game drops it on load."""

    def _save(self, filename):
        inv = lambda: {"Slots": [], "ValidSlotIndices": [{"X": x, "Y": 0} for x in range(12)],
                       "Class": {"InventoryClass": "C"}, "BaseStatValues": []}
        entry = {"Resource": {"Filename": filename, "Seed": [True, "0x0"]},
                 "Inventory": inv(), "Inventory_TechOnly": inv(), "Inventory_Cargo": inv()}
        return {"BaseContext": {"PlayerStateData": {
            "ShipOwnership": [entry], "KnownTech": [],
            "CharacterCustomisationData": [{} for _ in range(26)]}}}

    def test_sentinel_core_tech_is_added_to_known_tech(self):
        from nms_save import ships
        save = self._save("MODELS/COMMON/SPACECRAFT/SENTINELSHIP/SENTINELSHIP_PROC.SCENE.MBIN")
        ships.set_core_tech(save, 0, True)
        known = {k.lstrip("^") for k in save["BaseContext"]["PlayerStateData"]["KnownTech"]}
        self.assertTrue({"HYPERDRIVE_ROBO", "SHIPSHIELD_ROBO", "LIFESUP_ROBO"} <= known)

    def test_special_ship_tech_is_added_to_known_tech(self):
        from nms_save import ships
        save = self._save("")  # empty slot
        special = {"Name": "X", "Resource": {"Filename": "MODELS/X.SCENE.MBIN", "Seed": [True, "0x6"]},
                   "Inventory_TechOnly": {"Slots": [{"Id": "^SHIPLAS1"}, {"Id": "^UP_PULSE3"}]}}
        ships.install_special_ship(save, 0, special)
        known = {k.lstrip("^") for k in save["BaseContext"]["PlayerStateData"]["KnownTech"]}
        self.assertIn("SHIPLAS1", known)
        self.assertIn("UP_PULSE3", known)


class CrossPlatformPaths(unittest.TestCase):
    """Game/save discovery must work on Linux (Steam/Proton), not just Windows."""

    def test_looks_like_game_matches_folder_case_insensitively(self):
        from nms_save import gamepath
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "binaries"))            # lower-case, as a Linux install may be
            open(os.path.join(d, "binaries", "nms.exe"), "w").close()
            os.makedirs(os.path.join(d, "gamedata", "pcbanks"))
            self.assertTrue(gamepath.looks_like_game(d))
            self.assertFalse(gamepath.looks_like_game(os.path.join(d, "nope")))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_steam_libraries_includes_extra_drives_from_the_vdf(self):
        from nms_save import gamepath
        steam, extra = tempfile.mkdtemp(), tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(steam, "steamapps"))
            with open(os.path.join(steam, "steamapps", "libraryfolders.vdf"), "w") as f:
                f.write('"libraryfolders"{"0"{"path" "%s"}}' % extra.replace("\\", "\\\\"))
            orig = gamepath._steam_roots
            gamepath._steam_roots = lambda: [steam]
            try:
                libs = gamepath.steam_libraries()
            finally:
                gamepath._steam_roots = orig
            self.assertIn(steam, libs)
            self.assertIn(extra, libs)
        finally:
            shutil.rmtree(steam, ignore_errors=True)
            shutil.rmtree(extra, ignore_errors=True)

    def test_linux_save_roots_point_at_the_proton_prefix(self):
        from nms_save import gamepath, locator
        lib = tempfile.mkdtemp()
        try:
            pfx = os.path.join(lib, "steamapps", "compatdata", "275850", "pfx", "drive_c", "users",
                               "steamuser", "AppData", "Roaming", "HelloGames", "NMS", "st_1")
            os.makedirs(pfx)
            with open(os.path.join(pfx, "save.hg"), "wb") as f:
                f.write(b"x")
            keep_w, keep_libs = locator.WINDOWS, gamepath.steam_libraries
            locator.WINDOWS = False
            gamepath.steam_libraries = lambda: [lib]
            try:
                roots = locator.save_roots()
                saves = locator.find_saves()
            finally:
                locator.WINDOWS, gamepath.steam_libraries = keep_w, keep_libs
            self.assertTrue(any("compatdata" in r for r in roots), roots)
            self.assertEqual([s.slot_name for s in saves], ["save.hg"])
        finally:
            shutil.rmtree(lib, ignore_errors=True)


class SaveDiff(unittest.TestCase):
    """The change summary must show what really changed, and nothing else."""

    def diff(self, a, b, **kw):
        from nms_save import savediff
        return savediff.diff(a, b, **kw)

    def slot(self, x, y, item="^FUEL1", amount=10):
        return {"Id": item, "Amount": amount, "Index": {"X": x, "Y": y}}

    def test_a_scalar_change_is_one_entry(self):
        out, total = self.diff({"a": {"b": 1, "c": 2}}, {"a": {"b": 5, "c": 2}})
        self.assertEqual(total, 1)
        self.assertEqual(out, [{"path": ["a", "b"], "kind": "changed", "before": 1, "after": 5}])

    def test_inventory_slots_are_matched_by_grid_position(self):
        before = [self.slot(0, 0), self.slot(1, 0), self.slot(2, 0)]
        after = [self.slot(2, 0), self.slot(0, 0, amount=99), self.slot(1, 0)]  # reordered + one edit
        out, total = self.diff({"Slots": before}, {"Slots": after})
        self.assertEqual(total, 1, out)
        self.assertEqual(out[0]["path"], ["Slots", ("at", 0, 0), "Amount"])
        self.assertEqual((out[0]["before"], out[0]["after"]), (10, 99))

    def test_added_and_removed_slots(self):
        out, _ = self.diff({"Slots": [self.slot(0, 0), self.slot(1, 0)]},
                           {"Slots": [self.slot(0, 0), self.slot(3, 1, "^OXYGEN")]})
        kinds = {(d["kind"], d["path"][-1]) for d in out}
        self.assertEqual(kinds, {("removed", ("at", 1, 0)), ("added", ("at", 3, 1))})

    def test_many_list_additions_are_one_counted_entry(self):
        out, total = self.diff({"Known": ["^A"]}, {"Known": ["^A"] + [f"^N{i}" for i in range(25)]})
        self.assertEqual(total, 1)
        self.assertEqual(out[0]["kind"], "added")
        self.assertEqual(out[0]["count"], 25)
        self.assertEqual(out[0]["sample"], ["^N0", "^N1", "^N2"])

    def test_a_few_list_additions_are_listed_one_by_one(self):
        out, total = self.diff({"Known": ["^A"]}, {"Known": ["^A", "^B", "^C"]})
        self.assertEqual(total, 2)
        self.assertEqual([d["after"] for d in out], ["^B", "^C"])

    def test_dict_keys_added_and_removed(self):
        out, _ = self.diff({"keep": 1, "old": 2}, {"keep": 1, "new": 3})
        self.assertEqual({(d["kind"], d["path"][0]) for d in out}, {("removed", "old"), ("added", "new")})

    def test_limit_keeps_the_true_total(self):
        before = {f"k{i}": 0 for i in range(50)}
        out, total = self.diff(before, {k: 1 for k in before}, limit=5)
        self.assertEqual((len(out), total), (5, 50))

    def test_no_change_is_empty(self):
        tree = {"a": [1, {"b": [True, "0x1"]}]}
        self.assertEqual(self.diff(tree, json.loads(json.dumps(tree))), ([], 0))


class SaveDiffTypes(Base):
    def test_true_to_1_and_1_to_1_point_0_are_changes(self):
        from nms_save import savediff
        for b, a in ((True, 1), (1, 1.0), (0, False)):
            out, total = savediff.diff({"x": {"v": b}}, {"x": {"v": a}})
            self.assertEqual(total, 1, (b, a))
            self.assertEqual(out[0]["path"], ["x", "v"])

    def test_an_edited_ship_after_a_new_one_is_named_by_its_old_slot(self):
        from webapp import summary
        summary.items.catalogue = lambda: {}
        ship = lambda name, seed: {"Name": name, "Resource": {"Seed": [True, seed]}}
        before = {"BaseContext": {"PlayerStateData": {"ShipOwnership": [ship("Alpha", "0x1"), ship("Beta", "0x2")]}}}
        after = {"BaseContext": {"PlayerStateData": {"ShipOwnership": [
            ship("Gamma", "0x9"), ship("Alpha", "0x1"), ship("Beta", "0x3")]}}}
        rows, _ = summary.describe(before, after)
        changed = [r for r in rows if r["kind"] == "changed"]
        self.assertTrue(changed, rows)
        for r in changed:
            self.assertIn(r["area"], ("Ship slot 0 (Alpha)", "Ship slot 1 (Beta)"), rows)


class SaveSummaryLabels(Base):
    def test_labels_read_like_the_game(self):
        from webapp import summary

        def broken():
            raise self.gamefiles.GameFileError("no game data")
        summary.items.catalogue = broken
        ship = {"Name": "Iron Vulture", "Resource": {"Filename": "MODELS/X/FIGHTER_PROC.SCENE.MBIN",
                                                     "Seed": [True, "0x1"]}}
        before = {"BaseContext": {"PlayerStateData": {"Specials": 1, "ShipOwnership": [{}, ship],
                                                      "KnownTech": []}}}
        after = json.loads(json.dumps(before))
        ps = after["BaseContext"]["PlayerStateData"]
        ps["Specials"] = 500
        ps["ShipOwnership"][1]["Resource"]["Seed"] = [True, "0xABC"]
        ps["KnownTech"] = ["^HYPERDRIVE"]
        rows, total = summary.describe(before, after)  # item names unreadable: ids must still show
        self.assertEqual(total, 3)
        by = {(r["area"], r["what"]): r for r in rows}
        self.assertEqual(by[("Quicksilver", "")]["after"], "500")
        seed = by[("Ship slot 1 (Iron Vulture)", "Seed")]
        self.assertEqual((seed["before"], seed["after"]), ("0x1", "0xABC"))
        self.assertEqual(by[("Known blueprints", "#0")]["after"], "HYPERDRIVE")

    def test_slots_words_recipes_and_unnamed_ships_are_named(self):
        from webapp import common, summary
        summary.items.catalogue = lambda: {"CARBON": {"name": "Carbon"}}
        summary.extras.word_groups = lambda: {"ATLAS_PLEASE": 4}
        slot = lambda amount: {"Id": "^CARBON", "Amount": amount, "Index": {"X": 7, "Y": 6}}
        ship = {"Name": "", "Resource": {"Filename": common.model_filename("fighter"), "Seed": [True, "0x1"]}}
        before = {"BaseContext": {"PlayerStateData": {
            "Inventory": {"Slots": [slot(10)]}, "ShipOwnership": [ship],
            "KnownWordGroups": [], "KnownRefinerRecipes": []}}}
        after = json.loads(json.dumps(before))
        ps = after["BaseContext"]["PlayerStateData"]
        ps["Inventory"]["Slots"][0]["Amount"] = 3
        ps["ShipOwnership"][0]["Name"] = "Par2"
        ps["KnownWordGroups"] = [{"Group": "^ATLAS_PLEASE", "Races": [False] * 9}]
        ps["KnownRefinerRecipes"] = ["^REFINERECIPE_100"]
        rows, _ = summary.describe(before, after)
        text = [(r["area"], r["what"], r.get("before"), r.get("after")) for r in rows]
        self.assertIn(("Exosuit", "slot 7,6 (Carbon) › Amount", "10", "3"), text)
        self.assertIn(("Ship slot 0 (Fighter)", "Name", "(blank)", "'Par2'"), text)
        self.assertIn(("Known words", "#0", None, "Atlas word 'please'"), text)
        self.assertIn(("Known refiner recipes", "#0", None, "Refiner recipe 100"), text)

    def test_each_inventory_says_whose_it_is(self):
        from webapp import common, summary
        summary.items.catalogue = lambda: {"CARBON": {"name": "Carbon"}}
        slot = lambda amount: {"Slots": [{"Id": "^CARBON", "Amount": amount, "Index": {"X": 0, "Y": 0}}]}
        ship = {"Name": "Biggs", "Resource": {"Filename": common.model_filename("fighter"), "Seed": [True, "0x1"]},
                "Inventory_Cargo": slot(1)}
        before = {"BaseContext": {"PlayerStateData": {
            "Inventory_Cargo": slot(1), "Inventory_TechOnly": slot(1), "FreighterInventory": slot(1),
            "Chest3Inventory": slot(1), "ShipOwnership": [ship]}}}
        after = json.loads(json.dumps(before))
        ps = after["BaseContext"]["PlayerStateData"]
        for inv in (ps["Inventory_Cargo"], ps["Inventory_TechOnly"], ps["FreighterInventory"], ps["Chest3Inventory"],
                    ps["ShipOwnership"][0]["Inventory_Cargo"]):
            inv["Slots"][0]["Amount"] = 2
        areas = {(r["area"], r["what"]) for r in summary.describe(before, after)[0]}
        self.assertEqual(areas, {("Exosuit cargo", "slot 0,0 (Carbon) › Amount"),
                                 ("Exosuit technology", "slot 0,0 (Carbon) › Amount"),
                                 ("Freighter", "slot 0,0 (Carbon) › Amount"),
                                 ("Storage container 2", "slot 0,0 (Carbon) › Amount"),
                                 ("Ship slot 0 (Biggs)", "Cargo › slot 0,0 (Carbon) › Amount")})


class SaveDrafts(Base):
    """Edits collect in a draft; only "save" writes the file, and only what the player reviewed."""

    def setUp(self):
        super().setUp()
        from nms_save import codec, mapping
        from webapp import common
        self.codec, self.mapping = codec, mapping
        self.path = os.path.join(self.dir, "save3.hg")
        self.tree = {"BaseContext": {"PlayerStateData": {
            "Units": 100, "Nanites": 5, "Specials": 0,
            "ShipOwnership": [{"Name": "Old Faithful",
                               "Resource": {"Filename": common.model_filename("fighter"), "Seed": [True, "0x1234"]}}]}}}
        self.put(self.tree)
        self.running = False
        self.windows = []
        self.start_app()

    def start_app(self):
        """(Re)import the app as a fresh process would, keeping the same cache folder."""
        from webapp import app as appmod, common, drafts
        from webapp.routes import designer, player, ships
        self.common, self.drafts, self.designer = common, drafts, designer
        for mod in (common, designer, player, ships):
            if hasattr(mod, "game_running"):
                mod.game_running = lambda: self.running
        self.client = appmod.app.test_client()

    def restart(self):
        """Simulate closing the app and opening it again: drop every module, keep the files."""
        self.drafts.release_all()  # the old process ends: the OS frees its locks
        for mod in [m for m in list(sys.modules) if m.startswith(("nms_save", "nms_procgen", "webapp"))]:
            del sys.modules[mod]
        from nms_save import gamefiles
        gamefiles.CACHE = self.dir
        self.start_app()

    def tearDown(self):
        for p in self.windows:  # before the temp folder goes: Windows won't delete a file another process holds
            p.kill()
            p.wait()
            p.stdout.close()
        self.drafts.release_all()
        super().tearDown()

    def put(self, tree, path=None):
        text = json.dumps(self.mapping.obfuscate(tree), separators=(",", ":"))
        with open(path or self.path, "wb") as f:
            f.write(self.codec.encode(text, False, 1))

    def with_units(self, units):
        t = json.loads(json.dumps(self.tree))
        t["BaseContext"]["PlayerStateData"]["Units"] = units
        return t

    def disk(self, path=None):
        from nms_save import save
        return save.open_save(path or self.path).readable["BaseContext"]["PlayerStateData"]

    def raw(self):
        with open(self.path, "rb") as f:
            return f.read()

    def backups(self):
        return [f for f in os.listdir(self.dir) if f.endswith(".bak")]

    def post(self, url, **body):
        return self.client.post(url, json=body)

    def state(self, path=None):
        return self.client.get("/api/draft", query_string={"path": path or self.path}).get_json()

    def units_shown(self, path=None):
        return self.client.get("/api/player", query_string={"path": path or self.path}).get_json()["units"]

    def edit_units(self, units=999, path=None):
        r = self.post("/api/player", path=path or self.path, units=units)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    def save(self, rev=None, path=None):
        if rev is None:
            rev = self.state(path)["rev"]
        return self.post("/api/draft/save", path=path or self.path, rev=rev)

    def draft_files(self):
        d = os.path.join(self.dir, "drafts")
        return sorted(f for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def hold_lock_elsewhere(self, path=None):
        """A second process (another Shipwright window) holding this save's lock until it is killed."""
        import subprocess
        lock = self.drafts._file(self.drafts._key(path or self.path), ".lock")
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from webapp import oslock; "
                "fd = oslock.try_lock(sys.argv[2]); print('held' if fd is not None else 'busy', flush=True); "
                "time.sleep(60)")
        p = subprocess.Popen([sys.executable, "-c", code, self.ROOT, lock], stdout=subprocess.PIPE, text=True)
        self.windows.append(p)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def edit_in_another_window(self, path, units):
        """A whole second app process on the same cache: opens `path`, edits Units, and exits normally."""
        import subprocess
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from webapp import app; c = app.app.test_client(); "
                "p = sys.argv[2]; c.get('/api/draft', query_string={'path': p}); "
                "r = c.post('/api/player', json={'path': p, 'units': int(sys.argv[3])}); print(r.status_code)")
        out = subprocess.run([sys.executable, "-c", code, self.ROOT, path, str(units)], capture_output=True, text=True,
                             timeout=180, env={**os.environ, "NMS_TOOL_CACHE": self.dir})
        self.assertEqual(out.stdout.strip().splitlines()[-1:], ["200"], out.stderr[-2000:])

    # -- the basics --

    def test_an_edit_stays_in_the_draft(self):
        before = self.raw()
        data = self.edit_units()
        self.assertIsNone(data["backup"])
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.units_shown(), 999)
        st = self.state()
        self.assertEqual({k: st[k] for k in ("dirty", "changes", "stale", "resumed", "locked", "file")},
                         {"dirty": True, "changes": 1, "stale": False, "resumed": False, "locked": None,
                          "file": "save3.hg"})
        self.assertIn("game_running", st)
        self.assertEqual(len(self.draft_files()), 1, "unsaved edits are kept in a file")

    def test_review_lists_the_change(self):
        self.edit_units()
        r = self.post("/api/draft/review", path=self.path).get_json()
        self.assertEqual((r["rev"], r["total"], r["stale"], r["missing"]), (self.state()["rev"], 1, False, False))
        c = r["changes"][0]
        self.assertEqual((c["area"], c["before"], c["after"]), ("Units", "100", "999"))

    def test_nothing_to_review_says_so(self):
        self.units_shown()
        r = self.post("/api/draft/review", path=self.path).get_json()
        self.assertEqual((r["total"], r["changes"], r["missing"]), (0, [], True))

    def test_save_writes_the_draft(self):
        self.edit_units()
        r = self.save()
        self.assertEqual(r.status_code, 200, r.get_json())
        data = r.get_json()
        self.assertEqual(self.disk()["Units"], 999)
        self.assertEqual(len(self.backups()), 1)
        self.assertTrue(os.path.exists(data["backup"]))
        self.assertFalse(self.state()["dirty"])
        self.assertEqual(self.draft_files(), [], "a saved draft leaves no file behind")
        self.assertFalse([f for f in os.listdir(self.dir) if f.endswith(".tmp")], "atomic write cleans up")

    def test_a_later_edit_than_the_review_is_not_saved_blind(self):
        self.edit_units(1)
        rev = self.state()["rev"]
        self.edit_units(2)
        r = self.save(rev=rev)
        self.assertEqual(r.status_code, 400)
        self.assertIn("review again", r.get_json()["error"])
        self.assertEqual(self.disk()["Units"], 100)

    def test_an_old_review_cannot_save_newer_edits(self):
        # rev must never repeat: save, then edit again, then replay the first review's save
        self.edit_units(1)
        old = self.state()["rev"]
        self.assertEqual(self.save(rev=old).status_code, 200)
        self.edit_units(2)
        self.assertEqual(self.save(rev=old).status_code, 400)
        self.post("/api/draft/discard", path=self.path)
        self.edit_units(3)
        self.assertNotEqual(self.state()["rev"], old)
        self.assertEqual(self.save(rev=old).status_code, 400)
        self.assertEqual(self.disk()["Units"], 1)

    def test_a_save_changed_on_disk_is_not_overwritten(self):
        self.edit_units()
        self.put(self.with_units(7))  # the game saved meanwhile
        after_game = self.raw()
        self.assertTrue(self.state()["stale"])
        self.assertTrue(self.post("/api/draft/review", path=self.path).get_json()["stale"])
        r = self.save()
        self.assertEqual(r.status_code, 400)
        self.assertIn("changed on disk", r.get_json()["error"])
        self.assertEqual(self.raw(), after_game)
        self.assertEqual(self.backups(), [])

    def test_discard_goes_back_to_the_file(self):
        self.edit_units()
        self.post("/api/draft/discard", path=self.path)
        self.assertEqual(self.units_shown(), 100)
        self.assertFalse(self.state()["dirty"])
        self.assertEqual(self.draft_files(), [])

    def test_a_random_edit_saves_what_was_shown(self):
        r = self.post("/api/ship/reroll", path=self.path, slot=0).get_json()
        self.assertEqual(self.disk()["ShipOwnership"][0]["Resource"]["Seed"][1], "0x1234")
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.disk()["ShipOwnership"][0]["Resource"]["Seed"][1], r["seed"])

    def test_a_failed_edit_leaves_the_draft_alone(self):
        self.edit_units(500)
        r = self.post("/api/player", path=self.path, units=1, nanites=-1)  # units is set before nanites fails
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.units_shown(), 500)
        self.assertEqual(self.state()["changes"], 1)

    def test_game_running_blocks_the_save_but_keeps_the_edits(self):
        self.edit_units()  # editing while the game runs is fine: nothing is written
        self.running = True
        r = self.save()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.disk()["Units"], 100)
        self.assertTrue(self.state()["dirty"])
        self.running = False
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.disk()["Units"], 999)

    def test_a_clean_draft_follows_the_file(self):
        self.assertEqual(self.units_shown(), 100)
        self.put(self.with_units(7))
        os.utime(self.path, ns=(1, 1))  # make sure the stamp moves even on a coarse clock
        self.assertEqual(self.units_shown(), 7)

    def test_unsaved_edits_are_never_pushed_out(self):
        others = [os.path.join(self.dir, f"save{i}.hg") for i in (5, 6, 7)]
        for p in others:
            self.put(self.tree, p)
        for i, p in enumerate([self.path] + others[:2]):
            self.edit_units(10 + i, path=p)
        self.units_shown(others[2])  # open a fourth save
        self.assertLessEqual(len(self.drafts.DRAFTS), self.drafts.DRAFTS_KEEP)
        for i, p in enumerate([self.path] + others[:2]):
            self.assertTrue(self.state(p)["dirty"], p)
            self.assertEqual(self.units_shown(p), 10 + i)

    def test_import_lands_in_the_draft(self):
        r = self.post("/api/save/import", path=self.path, json=json.dumps(self.with_units(42)))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.disk()["Units"], 100)
        rows = self.post("/api/draft/review", path=self.path).get_json()["changes"]
        self.assertEqual([(c["area"], c["after"]) for c in rows], [("Units", "42")])

    def test_export_shows_the_draft(self):
        self.edit_units()
        r = self.client.get("/api/save/export", query_string={"path": self.path})
        self.assertEqual(json.loads(r.data)["BaseContext"]["PlayerStateData"]["Units"], 999)

    # -- two changes at once --

    def test_two_edits_at_the_same_time_both_land(self):
        import threading
        import time as clock
        from webapp.routes import player as pl
        real, gate = pl.write, threading.Event()

        def slow(opened):
            if opened.readable["BaseContext"]["PlayerStateData"]["Units"] == 111:
                gate.wait(5)
            return real(opened)
        pl.write = slow
        out = {}
        t1 = threading.Thread(target=lambda: out.setdefault(1, self.post("/api/player", path=self.path, units=111)))
        t2 = threading.Thread(target=lambda: out.setdefault(2, self.post("/api/player", path=self.path, nanites=222)))
        t1.start()
        clock.sleep(0.3)
        t2.start()
        clock.sleep(0.3)
        gate.set()
        t1.join(10)
        t2.join(10)
        pl.write = real
        self.assertEqual((out[1].status_code, out[2].status_code), (200, 200))
        shown = self.client.get("/api/player", query_string={"path": self.path}).get_json()
        self.assertEqual((shown["units"], shown["nanites"]), (111, 222), "neither edit may be lost")
        self.assertEqual(self.state()["changes"], 2)

    def test_an_edit_made_on_an_older_file_is_refused(self):
        # A starts on the file; the game saves; B starts on the new file and lands; then A lands.
        # A's copy is of the old file, so it must not slip in and hide that the file changed.
        common = self.common
        self.units_shown()
        with self.client.application.test_request_context("/x", method="POST"):
            a = common.open_save(self.path)
        self.put(self.with_units(7))
        os.utime(self.path, ns=(5, 5))
        with self.client.application.test_request_context("/x", method="POST"):
            b = common.open_save(self.path)
            b.readable["BaseContext"]["PlayerStateData"]["Nanites"] = 9
            common.write(b)
        with self.client.application.test_request_context("/x", method="POST"):
            a.readable["BaseContext"]["PlayerStateData"]["Specials"] = 3
            with self.assertRaises(ValueError):
                common.write(a)
        self.assertEqual(self.save().status_code, 200)
        disk = self.disk()
        self.assertEqual((disk["Units"], disk["Nanites"], disk["Specials"]), (7, 9, 0))

    # -- read-back and the design install --

    def test_a_put_ship_saves_and_reads_back(self):
        r = self.post("/api/ship/put", path=self.path, ship="fighter", seed="0xBEEF", slot="0")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.disk()["ShipOwnership"][0]["Resource"]["Seed"][1], "0x1234")
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(int(self.disk()["ShipOwnership"][0]["Resource"]["Seed"][1], 16), 0xBEEF)

    def test_editing_a_put_ship_again_is_not_a_false_alarm(self):
        self.post("/api/ship/put", path=self.path, ship="fighter", seed="0xBEEF", slot="0")
        seed = self.post("/api/ship/reroll", path=self.path, slot=0).get_json()["seed"]
        r = self.save()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.disk()["ShipOwnership"][0]["Resource"]["Seed"][1], seed)

    def test_a_save_that_does_not_read_back_is_reported_and_kept(self):
        self.edit_units()
        real = self.drafts._read_back
        self.drafts._read_back = lambda path, raw: "{}"  # the file comes back different
        r = self.save()
        self.drafts._read_back = real
        self.assertEqual(r.status_code, 500)
        data = r.get_json()
        self.assertIn("does not read back", data["error"])
        self.assertTrue(os.path.exists(data["backup"]))
        st = self.state()
        self.assertTrue(st["dirty"], "the edits stay so the player can retry or discard")
        self.assertFalse(st["stale"], "the file we just wrote is not someone else's change")
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.disk()["Units"], 999)

    def test_the_install_job_edits_from_its_own_thread(self):
        import threading
        self.units_shown()
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.designer.put_design(
            self.path, "fighter", None, "0x00000000000000AB", "0", "Job ship")))
        t.start()
        t.join(10)
        self.assertEqual(out["r"]["slot"], 0)
        self.assertEqual(self.disk()["ShipOwnership"][0]["Resource"]["Seed"][1], "0x1234")
        self.assertEqual(self.state()["changes"], 1)

    # -- backups --

    def test_restore_is_previewed_and_refused_with_unsaved_edits(self):
        backup = self.path + ".20260101-000000-000000.bak"
        self.put(self.with_units(1), backup)
        current = self.raw()
        self.edit_units()
        r = self.post("/api/backups/restore", path=self.path, file=backup, preview=True)
        self.assertEqual(r.status_code, 400)
        self.assertIn("unsaved changes", r.get_json()["error"])
        self.post("/api/draft/discard", path=self.path)

        r = self.post("/api/backups/restore", path=self.path, file=backup, preview=True)
        self.assertEqual(r.status_code, 200, r.get_json())
        p = r.get_json()["preview"]
        self.assertEqual((p["changes"][0]["before"], p["changes"][0]["after"]), ("100", "1"))
        self.assertEqual(self.raw(), current)
        done = self.post("/api/save/commit", token=p["token"]).get_json()
        self.assertEqual(self.disk()["Units"], 1)
        self.assertEqual(self.units_shown(), 1, "the page now edits the restored file")
        with open(done["safety_copy"], "rb") as f:
            self.assertEqual(f.read(), current)

    def test_restore_commit_refuses_edits_made_after_the_preview(self):
        backup = self.path + ".20260101-000000-000000.bak"
        self.put(self.with_units(1), backup)
        r = self.post("/api/backups/restore", path=self.path, file=backup, preview=True)
        token = r.get_json()["preview"]["token"]
        self.edit_units(555)
        r = self.post("/api/save/commit", token=token)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.disk()["Units"], 100)
        self.assertEqual(self.units_shown(), 555, "the edit is kept")

    def test_restore_writes_what_the_preview_showed(self):
        backup = self.path + ".20260101-000000-000000.bak"
        self.put(self.with_units(1), backup)
        r = self.post("/api/backups/restore", path=self.path, file=backup, preview=True)
        token = r.get_json()["preview"]["token"]
        self.put(self.with_units(2), backup)  # the backup changes before the player confirms
        self.assertEqual(self.post("/api/save/commit", token=token).status_code, 200)
        self.assertEqual(self.disk()["Units"], 1)

    def test_restore_always_needs_confirming(self):
        backup = self.path + ".20260101-000000-000000.bak"
        self.put(self.with_units(1), backup)
        before = self.raw()
        r = self.post("/api/backups/restore", path=self.path, file=backup)  # no "preview" flag
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.raw(), before, "nothing is replaced until the summary is confirmed")
        token = r.get_json()["preview"]["token"]
        self.assertEqual(self.post("/api/save/commit", token=token).status_code, 200)
        self.assertEqual(self.disk()["Units"], 1)

    def test_only_this_saves_backups_can_be_restored(self):
        other = os.path.join(self.dir, "other.bin")
        self.put(self.with_units(1), other)
        elsewhere = os.path.join(self.dir, "elsewhere")
        os.makedirs(elsewhere)
        lookalike = os.path.join(elsewhere, "save3.hg.20260101-000000-000000.bak")
        self.put(self.with_units(1), lookalike)
        sneaky = self.path + ".x" + os.sep + ".." + os.sep + "other.bin"
        os.makedirs(self.path + ".x")
        for f in (other, sneaky, lookalike, self.path + ".tmp", self.path):
            r = self.post("/api/backups/restore", path=self.path, file=f)
            self.assertEqual(r.status_code, 400, f)
            self.assertIn("Unknown backup", r.get_json()["error"])
        self.assertEqual(self.disk()["Units"], 100)

    def test_a_refused_restore_keeps_its_token(self):
        backup = self.path + ".20260101-000000-000000.bak"
        self.put(self.with_units(1), backup)
        token = self.post("/api/backups/restore", path=self.path, file=backup).get_json()["preview"]["token"]
        self.edit_units(555)
        self.assertEqual(self.post("/api/save/commit", token=token).status_code, 400)
        self.post("/api/draft/discard", path=self.path)
        r = self.post("/api/save/commit", token=token)
        self.assertEqual(r.status_code, 200, "the player discarded their edits and confirms again")
        self.assertEqual(self.disk()["Units"], 1)

    # -- surviving a restart --

    def test_unsaved_edits_survive_a_restart(self):
        self.edit_units(321)
        self.restart()
        st = self.state()
        self.assertEqual((st["dirty"], st["changes"], st["resumed"], st["stale"]), (True, 1, True, False))
        self.assertEqual(self.units_shown(), 321)
        self.assertEqual([d["file"] for d in self.client.get("/api/drafts").get_json()], ["save3.hg"])
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.disk()["Units"], 321)

    def test_resumed_edits_on_a_changed_file_are_stale(self):
        self.edit_units(321)
        self.restart()
        self.put(self.with_units(7))  # the game played and saved while the app was closed
        self.assertTrue(self.state()["stale"])
        self.assertEqual(self.save().status_code, 400)
        self.assertEqual(self.disk()["Units"], 7)

    def test_drafts_lists_saves_with_unsaved_edits(self):
        other = os.path.join(self.dir, "save5.hg")
        self.put(self.tree, other)
        self.units_shown(other)
        self.edit_units()
        rows = self.client.get("/api/drafts").get_json()
        self.assertEqual([(r["file"], r["changes"], r["locked"]) for r in rows], [("save3.hg", 1, None)])

    # -- another Shipwright window --

    def test_a_save_open_in_another_window_is_read_only(self):
        other = self.hold_lock_elsewhere()
        self.assertTrue(self.state()["locked"])
        self.assertEqual(self.units_shown(), 100, "looking still works: it shows the file")
        for url, body in (("/api/player", {"units": 1}), ("/api/draft/discard", {}),
                          ("/api/draft/save", {"rev": "x"}), ("/api/draft/review", {})):
            r = self.post(url, path=self.path, **body)
            self.assertEqual(r.status_code, 423, url)
            self.assertIn("another Shipwright window", r.get_json()["error"])
        self.assertEqual(self.disk()["Units"], 100)
        self.assertIsNone(other.poll(), "checking the lock must not stop the other window")

    def test_a_window_that_crashed_leaves_no_lock(self):
        other = self.hold_lock_elsewhere()
        self.assertTrue(self.state()["locked"])
        other.kill()
        other.wait()
        self.assertIsNone(self.state()["locked"])
        self.edit_units()
        self.assertTrue(self.state()["dirty"])

    def test_a_leftover_lock_file_is_no_lock(self):
        lock = self.drafts._file(self.drafts._key(self.path), ".lock")
        for content in (b"", b"{garbage", json.dumps({"pid": os.getpid() + 1, "host": "elsewhere"}).encode()):
            with open(lock, "wb") as f:
                f.write(content)
            self.assertIsNone(self.state()["locked"], content)
            self.edit_units()
            self.post("/api/draft/discard", path=self.path)
            self.units_shown(os.path.join(self.dir, "save3.hg"))
            self.drafts.release(self.drafts._key(self.path))

    def test_another_windows_edits_are_seen_after_it_closes(self):
        # This window looks at X, then moves to Y (letting go of X). Another window edits X and
        # closes. Coming back to X must show those edits, and editing on must keep them.
        y = os.path.join(self.dir, "save5.hg")
        self.put(self.tree, y)
        self.assertEqual(self.units_shown(), 100)
        self.state(y)
        self.edit_in_another_window(self.path, 777)
        st = self.state()
        self.assertEqual((st["dirty"], st["changes"], st["locked"]), (True, 1, None))
        self.assertEqual(self.units_shown(), 777)
        self.assertEqual([r["file"] for r in self.client.get("/api/drafts").get_json()], ["save3.hg"])
        self.assertEqual(self.post("/api/player", path=self.path, nanites=9).status_code, 200)
        self.assertEqual(self.save().status_code, 200)
        disk = self.disk()
        self.assertEqual((disk["Units"], disk["Nanites"]), (777, 9))

    def test_a_read_back_that_fails_is_reported_and_not_stale_after_restart(self):
        self.edit_units(5)
        real = self.drafts._read_back

        def boom(path, raw):
            raise ValueError("decode failed")
        self.drafts._read_back = boom
        r = self.save()
        self.drafts._read_back = real
        self.assertEqual(r.status_code, 500)
        self.assertIn("couldn't check the file it just wrote", r.get_json()["error"])
        self.assertIn(os.path.basename(r.get_json()["backup"]), r.get_json()["error"])
        self.assertTrue(os.path.exists(r.get_json()["backup"]))
        self.assertEqual(self.disk()["Units"], 5, "the file was written")
        self.restart()
        st = self.state()
        self.assertEqual((st["dirty"], st["stale"]), (True, False), "our own write is not someone else's change")
        self.assertEqual(self.save().status_code, 200)

    def test_a_draft_file_of_the_wrong_shape_is_set_aside(self):
        self.edit_units()
        self.restart()
        key = self.drafts._key(self.path)
        for bad in ({"path": self.path}, {"path": self.path, "base": 1, "gen": "g", "version": 1, "saved_version": 0,
                                          "chunked": False, "trailing_nul": 1, "tree": []}, [1, 2]):
            with open(self.drafts._file(key, ".json"), "w", encoding="utf-8") as f:
                json.dump(bad, f)
            self.drafts.DRAFTS.clear()
            r = self.client.get("/api/player", query_string={"path": self.path})
            self.assertEqual(r.status_code, 200, bad)
            self.assertEqual(r.get_json()["units"], 100)
            self.assertTrue(os.path.exists(self.drafts._file(key, ".broken")), "kept aside, not deleted")
            self.assertFalse(self.state()["dirty"])

    def test_edits_on_a_stale_draft_are_refused(self):
        self.edit_units(1)
        self.put(self.with_units(7))  # the game saved: these edits can never be written
        r = self.post("/api/player", path=self.path, nanites=3)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], self.drafts.STALE_MSG, "say why, and that Discard is the way out")
        self.assertEqual(self.state()["changes"], 1)

    def test_discard_with_an_old_rev_is_refused(self):
        self.edit_units(1)
        rev = self.state()["rev"]
        self.edit_units(2)
        r = self.post("/api/draft/discard", path=self.path, rev=rev)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.units_shown(), 2)
        self.assertEqual(self.post("/api/draft/discard", path=self.path, rev=self.state()["rev"]).status_code, 200)
        self.assertEqual(self.units_shown(), 100)

    def test_drafts_from_this_session_are_not_called_resumed(self):
        others = [os.path.join(self.dir, f"save{i}.hg") for i in (5, 6)]
        for p in others:
            self.put(self.tree, p)
        for p in [self.path] + others:  # three drafts: the first is pushed out of memory
            self.edit_units(path=p)
        self.assertFalse(self.state()["resumed"])
        self.restart()
        self.assertTrue(self.state()["resumed"])

    def test_not_knowing_if_the_game_runs_blocks_the_save(self):
        self.edit_units()
        self.common.game_running = lambda: None
        r = self.save()
        self.assertEqual(r.status_code, 409)
        self.assertIn("Could not check", r.get_json()["error"])
        self.assertEqual(self.disk()["Units"], 100)
        self.assertTrue(self.state()["dirty"])

    def test_a_file_that_keeps_changing_while_read_is_refused(self):
        from unittest import mock
        real, n = os.stat, [0]

        def moving(path, *a, **k):
            st = real(path, *a, **k)
            if str(path) == self.path:
                n[0] += 1
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                       st.st_size + n[0], st.st_atime, st.st_mtime, st.st_ctime))
            return st
        with mock.patch("os.stat", moving):
            with self.assertRaises(ValueError) as err:
                self.drafts._read_stable(self.path)
        self.assertIn("keeps changing", str(err.exception))

    def test_closing_never_waits_for_a_save(self):
        import threading
        self.edit_units()
        held, done = threading.Event(), threading.Event()

        def busy():
            with self.drafts.DRAFTS_LOCK:
                held.set()
                done.wait(5)
        t = threading.Thread(target=busy)
        t.start()
        held.wait(5)
        try:
            self.assertTrue(self.drafts.dirty_any())
        finally:
            done.set()
            t.join(5)

    def test_other_web_pages_cannot_change_a_save(self):
        for headers in ({"Origin": "http://evil.example"}, {"Origin": "null"},
                        {"Referer": "http://evil.example/page"}, {"Sec-Fetch-Site": "cross-site"}):
            r = self.client.post("/api/player", json={"path": self.path, "units": 1}, headers=headers)
            self.assertEqual(r.status_code, 403, headers)
        r = self.client.get("/api/player", query_string={"path": self.path}, headers={"Host": "evil.example"})
        self.assertEqual(r.status_code, 403, "a DNS name pointed at this PC must not read saves")
        self.assertFalse(self.state()["dirty"])
        r = self.client.post("/api/player", json={"path": self.path, "units": 1},
                             headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 200, "the app's own page still works")

    def test_closing_mid_save_asks_with_its_own_question(self):
        import nms_editor

        class Window:
            confirm_close = None
            localization = {}
        w = Window()
        self.units_shown()
        seen = []
        real = self.drafts._read_back

        def check(path, raw):  # runs while the save is being written
            seen.append(self.drafts.saving())
            seen.append((nms_editor.confirm_close(w), w.confirm_close, w.localization["global.quitConfirmation"]))
            return real(path, raw)
        self.drafts._read_back = check
        self.edit_units()
        r = self.save()
        self.drafts._read_back = real
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(seen[0], "a save in progress is known")
        self.assertEqual(seen[1], (True, True, nms_editor.QUIT_WHILE_SAVING))
        self.assertFalse(self.drafts.saving(), "and forgotten once it ends")
        nms_editor.confirm_close(w)
        self.assertFalse(w.confirm_close, "nothing unsaved and nothing writing: close without asking")
        self.assertEqual(w.localization["global.quitConfirmation"], nms_editor.QUIT_WITH_EDITS)

    def test_a_restore_in_progress_counts_as_saving(self):
        self.edit_units(5)
        self.assertEqual(self.save().status_code, 200)
        bak = os.path.join(self.dir, self.backups()[0])
        token = self.post("/api/backups/restore", path=self.path, file=bak).get_json()["preview"]["token"]
        from nms_save import save as savemod
        real, seen = savemod.write_atomic, []

        def spy(path, raw):
            seen.append(self.drafts.saving())
            return real(path, raw)
        savemod.write_atomic = spy
        try:
            r = self.post("/api/save/commit", token=token)
        finally:
            savemod.write_atomic = real
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(seen, [True])
        self.assertFalse(self.drafts.saving())

    def test_a_temp_file_left_by_a_cut_off_save_is_cleared(self):
        from nms_save import save as savemod
        tmp = savemod.tmp_path_for(self.path)
        other = self.path + ".tmp2"
        for f in (tmp, other):
            with open(f, "wb") as fh:
                fh.write(b"half a save")
        self.units_shown()  # opening the save
        self.assertFalse(os.path.exists(tmp), "write_atomic's own temp file goes")
        self.assertTrue(os.path.exists(other), "nothing else is touched")
        self.assertEqual(self.units_shown(), 100)

    def test_listing_drafts_leaves_their_saves_free_for_other_windows(self):
        self.edit_units()
        self.restart()
        listed = self.client.get("/api/drafts").get_json()
        self.assertEqual([(d["file"], d["locked"]) for d in listed], [("save3.hg", None)])
        self.assertNotIn(self.drafts._key(self.path), self.drafts.HELD, "only looked, didn't take it")
        self.hold_lock_elsewhere()  # another window can still open it
        listed = self.client.get("/api/drafts").get_json()
        self.assertTrue(listed[0]["locked"], "and the list now says it is open elsewhere")
        self.assertTrue(self.state()["locked"])

    def test_letting_go_of_a_save_unlocks_it_while_holding_the_bookkeeping_lock(self):
        other = os.path.join(self.dir, "save4.hg")
        self.put(self.tree, other)
        self.units_shown()
        self.state()  # this save is selected and held
        from webapp import oslock
        real, seen = oslock.unlock, []

        def spy(fd):
            seen.append(self.drafts.DRAFTS_LOCK._is_owned())
            return real(fd)
        oslock.unlock = spy
        try:
            self.state(other)  # moving to another save lets go of this clean one
        finally:
            oslock.unlock = real
        self.assertEqual(seen, [True])
        self.assertNotIn(self.drafts._key(self.path), self.drafts.HELD)
        self.hold_lock_elsewhere()

    def test_closing_the_window_asks_only_with_unsaved_edits(self):
        import nms_editor

        class Window:
            confirm_close = None
        w = Window()
        self.units_shown()
        self.assertTrue(nms_editor.confirm_close(w))
        self.assertFalse(w.confirm_close)
        self.edit_units()
        self.assertTrue(nms_editor.confirm_close(w), "the window itself asks; the handler never blocks")
        self.assertTrue(w.confirm_close)


if __name__ == "__main__":
    unittest.main(verbosity=2)
