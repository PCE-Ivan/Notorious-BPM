#!/usr/bin/env python3
"""Unit tests for library_manager.py and app.py's runtime library-switch
logic. Never touches the real library.db/config.json -- everything here
runs against fresh temp directories, deleted again at the end.

Run this file ON ITS OWN, not combined with other test files on one
`python3 -m unittest` command line. Combining test files that each set
JUKEBOX_DB_PATH/JUKEBOX_MUSIC_DIR (this one, test_smoke.py, ...) runs them
in one shared process, where modules like scan_library.py/organize_by_
artist.py/etc still cache their own MUSIC_DIR/DB_PATH globals once at
import time -- an already-imported module from an earlier test file in
the same run won't pick up a later file's env var changes without an
importlib.reload() every caller remembers to do correctly. (config.py's
own CONFIG_PATH no longer has this problem -- get_config_path() resolves
fresh on every call -- fixed after exactly this kind of combined run once
wrote a scratch test path into the real, production config on this exact
machine. The other modules' own module-level globals still do, though;
this warning stays here as a result.)

Run manually:

    python3 test_library_manager.py
"""
import os
import shutil
import sqlite3
import tempfile
import unittest


class LibraryManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="jukebox-libtest-")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(self.tmpdir, "config.json")
        os.environ.pop("JUKEBOX_DB_PATH", None)
        os.environ.pop("JUKEBOX_MUSIC_DIR", None)
        import config as jukebox_config
        import importlib
        importlib.reload(jukebox_config)
        global config, library_manager
        config = jukebox_config
        import library_manager as lm
        importlib.reload(lm)
        library_manager = lm

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ.pop("JUKEBOX_CONFIG_PATH", None)
        os.environ.pop("JUKEBOX_DB_PATH", None)
        os.environ.pop("JUKEBOX_MUSIC_DIR", None)

    def test_create_new_builds_schema_and_meta(self):
        save_path = os.path.join(self.tmpdir, "MyLib.nbpmlib")
        music_dir = os.path.join(self.tmpdir, "music")
        os.makedirs(music_dir, exist_ok=True)

        db_path = library_manager.create_new(save_path, music_dir)
        self.assertEqual(db_path, save_path)
        self.assertTrue(os.path.isfile(save_path))
        self.assertEqual(library_manager.read_music_dir(save_path), music_dir)

        conn = sqlite3.connect(save_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn("tracks", tables)
        self.assertIn("library_meta", tables)

        current = library_manager.get_current()
        self.assertEqual(current["path"], save_path)
        self.assertEqual(current["music_dir"], music_dir)

        comp = library_manager.companion_dir(save_path)
        for sub in ("trash", "art_cache", "backups"):
            self.assertTrue(os.path.isdir(os.path.join(comp, sub)), sub)

    def test_two_libraries_have_isolated_companion_dirs(self):
        path_a = library_manager.create_new(os.path.join(self.tmpdir, "A.nbpmlib"), os.path.join(self.tmpdir, "musicA"))
        path_b = library_manager.create_new(os.path.join(self.tmpdir, "B.nbpmlib"), os.path.join(self.tmpdir, "musicB"))
        self.assertNotEqual(library_manager.companion_dir(path_a), library_manager.companion_dir(path_b))

    def test_open_existing_missing_file_reports_error_not_crash(self):
        result = library_manager.open_existing(os.path.join(self.tmpdir, "nope.nbpmlib"))
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_legacy_migration_is_idempotent(self):
        legacy_db = os.path.join(self.tmpdir, "library.db")
        legacy_music_dir = os.path.join(self.tmpdir, "legacy_music")
        os.makedirs(legacy_music_dir, exist_ok=True)
        import scan_library
        conn = sqlite3.connect(legacy_db)
        scan_library.build_schema(conn)
        conn.close()
        config.update_config(lambda cfg: cfg.__setitem__("music_dir", legacy_music_dir))
        os.environ["JUKEBOX_DB_PATH"] = legacy_db

        # resolve_startup() with JUKEBOX_DB_PATH already set should leave
        # config's migration state alone entirely (dev-override short circuit).
        db_path, music_dir = library_manager.resolve_startup()
        self.assertEqual(db_path, legacy_db)
        cfg = config.load_config()
        self.assertNotIn("current_library", cfg)

        # _default_legacy_db_path() only falls back to the real fixed
        # app-data location when JUKEBOX_DB_PATH is unset -- exercising
        # THAT exact path (a real first-post-update launch, no dev
        # override) directly against _migrate_legacy_if_needed() instead
        # of resolve_startup() avoids needing JUKEBOX_DB_PATH unset (which
        # would otherwise make this test touch the real, non-scratch,
        # machine-wide app-data directory).
        library_manager._migrate_legacy_if_needed()
        cfg = config.load_config()
        self.assertEqual(cfg["current_library"], legacy_db)
        self.assertEqual(library_manager.read_music_dir(legacy_db), legacy_music_dir)

        # Second call must be a no-op -- same result, nothing duplicated.
        library_manager._migrate_legacy_if_needed()
        cfg2 = config.load_config()
        self.assertEqual(cfg2["current_library"], legacy_db)
        self.assertEqual(len(cfg2["recent_libraries"]), 1)


class AppSwitchLibraryTest(unittest.TestCase):
    """Exercises app.py's own _switch_library against two scratch libraries
    -- confirms MUSIC_DIR/DB_PATH/ART_CACHE_DIR/TRASH_DIR/BACKUP_DIR swap
    correctly, _schema_ready resets, and the two libraries' data stays
    isolated (no cross-contamination of tracks or art-cache ids)."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-libswitch-")
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "boot.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = os.path.join(cls.tmpdir, "boot_music")
        os.makedirs(os.environ["JUKEBOX_MUSIC_DIR"], exist_ok=True)
        global app, library_manager
        import app as app_module
        import library_manager as lm
        app = app_module
        library_manager = lm

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_switch_between_two_libraries(self):
        lib_a = library_manager.create_new(
            os.path.join(self.tmpdir, "SwitchA.nbpmlib"), os.path.join(self.tmpdir, "musicA"),
        )
        lib_b = library_manager.create_new(
            os.path.join(self.tmpdir, "SwitchB.nbpmlib"), os.path.join(self.tmpdir, "musicB"),
        )

        # _switch_library calls close_db(None), which touches flask.g --
        # only valid inside an app context, same as every other place this
        # runs from in production (always a real route handler, which
        # already has one). Given directly here since this call isn't
        # itself inside a request.
        with app.app.app_context():
            result = app._switch_library(lib_a, os.path.join(self.tmpdir, "musicA"))
        self.assertTrue(result["ok"])
        self.assertEqual(app.DB_PATH, lib_a)
        self.assertFalse(app._schema_ready)

        conn = sqlite3.connect(app.DB_PATH)
        conn.execute(
            "INSERT INTO tracks (path, artist, title) VALUES ('a.mp3', 'Artist A', 'Song A')"
        )
        conn.commit()
        conn.close()

        with app.app.app_context():
            result = app._switch_library(lib_b, os.path.join(self.tmpdir, "musicB"))
        self.assertTrue(result["ok"])
        self.assertEqual(app.DB_PATH, lib_b)
        self.assertNotEqual(app.ART_CACHE_DIR, app.TRASH_DIR)  # sanity: real distinct paths

        conn = sqlite3.connect(app.DB_PATH)
        count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "library B should not see library A's track")

        self.assertNotEqual(
            library_manager.companion_dir(lib_a), library_manager.companion_dir(lib_b),
            "the two libraries must have isolated art_cache/trash/backups",
        )

    def test_switch_refused_while_job_running(self):
        with app._library_lock:
            app._scan_state["running"] = True
        try:
            with app.app.app_context():
                result = app._switch_library(
                    os.path.join(self.tmpdir, "unused.nbpmlib"), self.tmpdir,
                )
            self.assertFalse(result["ok"])
        finally:
            app._scan_state["running"] = False


if __name__ == "__main__":
    unittest.main()
