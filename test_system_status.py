#!/usr/bin/env python3
"""Tests for system_status.py (library-access diagnosis) and the Flask error
handler / diagnostics / Activity-tray routes built on it. Everything runs
against a scratch library in a temp dir.

Run manually:

    python3 test_system_status.py
"""
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

import system_status


class LibraryProblemsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jukebox-sysstatus-")
        self.db = os.path.join(self.tmp, "lib.nbpmlib")
        self.music = os.path.join(self.tmp, "music")
        os.makedirs(self.music)
        with open(self.db, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\x00" * 100)

    def tearDown(self):
        if os.path.exists(self.db):
            os.chmod(self.db, 0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_healthy_library_has_no_problems(self):
        self.assertEqual(system_status.library_problems(self.db, self.music), [])

    def test_missing_library_file_reads_as_drive_problem(self):
        os.remove(self.db)
        problems = system_status.library_problems(self.db, self.music)
        self.assertEqual(problems[0]["kind"], "library_missing")
        self.assertIn("reconnect", problems[0]["message"])

    def test_unreadable_library_file_reads_as_macos_permission(self):
        os.chmod(self.db, 0)
        problems = system_status.library_problems(self.db, self.music)
        self.assertEqual(problems[0]["kind"], "macos_permission")
        self.assertEqual(problems[0]["fix"], "open_privacy_settings")

    def test_missing_music_folder_reads_as_drive_problem(self):
        shutil.rmtree(self.music)
        kinds = [p["kind"] for p in system_status.library_problems(self.db, self.music)]
        self.assertEqual(kinds, ["music_missing"])

    def test_describe_exception_prefers_the_real_diagnosis(self):
        os.remove(self.db)
        kind, message = system_status.describe_exception(RuntimeError("x"), self.db, self.music)
        self.assertEqual(kind, "library_missing")

    def test_describe_exception_generic_fallback_names_the_log(self):
        kind, message = system_status.describe_exception(ValueError("x"), self.db, self.music)
        self.assertEqual(kind, "internal")
        self.assertIn("log", message)

    def test_volume_that_isnt_mounted_is_reported_as_a_disconnected_drive(self):
        problem = system_status.music_problem("/Volumes/DefinitelyNotMountedDrive-12345/Music")
        self.assertEqual(problem["kind"], "music_missing")
        self.assertIn("DefinitelyNotMountedDrive-12345", problem["message"])
        self.assertIn("isn't connected", problem["message"])

    def test_blocker_text_for_missing_unset_and_healthy_music_folder(self):
        self.assertIn("Set a music folder", system_status.music_dir_blocker(None))
        shutil.rmtree(self.music)
        self.assertIn("isn't reachable", system_status.music_dir_blocker(self.music))
        os.makedirs(self.music)
        self.assertIsNone(system_status.music_dir_blocker(self.music))

    def test_describe_exception_corrupt_database(self):
        kind, _ = system_status.describe_exception(
            sqlite3.DatabaseError("database disk image is malformed"), self.db, self.music)
        self.assertEqual(kind, "library_corrupt")


class PendingPromptTest(unittest.TestCase):
    """A first-time open of a protected folder blocks inside the kernel until
    the macOS consent dialog is answered. That used to look like a healthy
    library with every query failing "database is locked"."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jukebox-prompt-")
        self.db = os.path.join(self.tmp, "lib.nbpmlib")
        with open(self.db, "wb") as f:
            f.write(b"SQLite format 3\x00" + b"\x00" * 100)
        self.music = os.path.join(self.tmp, "music")
        os.makedirs(self.music)
        self.release = threading.Event()
        old_timeout = system_status.PROBE_TIMEOUT
        system_status.PROBE_TIMEOUT = 0.2
        self.addCleanup(setattr, system_status, "PROBE_TIMEOUT", old_timeout)
        self.addCleanup(self.release.set)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(system_status._pending.clear)

    def block_open_of(self, folder):
        real_open = os.open

        def blocking(path, *a, **k):
            if os.path.realpath(path) == os.path.realpath(folder):
                self.release.wait(10)      # what the kernel does until the dialog is answered
            return real_open(path, *a, **k)

        return mock.patch.object(system_status.os, "open", side_effect=blocking)

    def probe_threads(self):
        return [t for t in threading.enumerate() if t.name == "access-probe" and t.is_alive()]

    def test_blocked_library_folder_is_explained_instead_of_looking_healthy(self):
        with self.block_open_of(self.tmp):
            started = time.time()
            problems = system_status.library_problems(self.db, self.music)
            self.assertLess(time.time() - started, 2)
            self.assertEqual([p["kind"] for p in problems], ["macos_prompt_pending"])
            self.assertIn("Allow", problems[0]["message"])
            self.assertIn(os.path.basename(self.tmp), problems[0]["message"])

            # polling while it's still stuck neither waits again nor piles up threads
            started = time.time()
            for _ in range(5):
                self.assertEqual(system_status.library_problems(self.db, self.music)[0]["kind"], "macos_prompt_pending")
            self.assertLess(time.time() - started, 0.2)
            self.assertEqual(len(self.probe_threads()), 1)

            # the same problem is what a failed request is explained with
            kind, message = system_status.describe_exception(sqlite3.OperationalError("database is locked"), self.db, self.music)
            self.assertEqual(kind, "macos_prompt_pending")

            self.release.set()                   # the user clicks Allow
            for t in self.probe_threads():
                t.join(2)
        self.assertEqual(system_status.library_problems(self.db, self.music), [])

    def test_blocked_music_folder_is_explained_too(self):
        real_scandir = os.scandir
        release = self.release

        def blocking(path="."):
            if os.path.realpath(path) == os.path.realpath(self.music):
                release.wait(10)
            return real_scandir(path)

        with mock.patch.object(system_status.os, "scandir", side_effect=blocking):
            problem = system_status.music_problem(self.music)
            self.assertEqual(problem["kind"], "macos_prompt_pending")
            self.assertIn("Allow", system_status.music_dir_blocker(self.music))
            self.release.set()
            for t in self.probe_threads():
                t.join(2)
        self.assertIsNone(system_status.music_problem(self.music))

    def test_a_fast_folder_open_costs_nothing(self):
        started = time.time()
        self.assertEqual(system_status.library_problems(self.db, self.music), [])
        self.assertLess(time.time() - started, 0.5)
        self.assertEqual(self.probe_threads(), [])


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-sysroutes-")
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.tmpdir
        import app as app_module
        cls.app_module = app_module
        cls.client = app_module.app.test_client()
        cls.client.get("/api/facets")  # creates the scratch DB file

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_status_ok_for_scratch_library(self):
        data = self.client.get("/api/system/status").get_json()
        self.assertTrue(data["ok"], data)

    def test_unhandled_error_becomes_readable_json_not_html(self):
        with mock.patch.object(self.app_module, "get_db", side_effect=ZeroDivisionError("boom")):
            resp = self.client.get("/api/facets")
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["kind"], "internal")
        self.assertIn("ZeroDivisionError", body["error"])

    def test_http_errors_still_pass_through_untouched(self):
        self.assertEqual(self.client.get("/api/definitely-not-a-route").status_code, 404)

    def test_diagnostics_redacts_secrets_and_includes_log_tail(self):
        self.app_module.jukebox_config.update_config(lambda c: c.__setitem__("acoustidApiKey", "SUPERSECRET"))
        data = self.client.get("/api/diagnostics").get_json()
        self.assertNotIn("SUPERSECRET", data["text"])
        self.assertEqual(data["info"]["config"]["acoustidApiKey"], "(set)")
        self.assertIn("recent log", data["text"])

    def test_file_touching_jobs_refuse_to_start_when_the_music_folder_is_unreachable(self):
        with mock.patch.object(self.app_module, "MUSIC_DIR", "/Volumes/DefinitelyNotMountedDrive-12345/Music"):
            for method, url, body in (
                ("post", "/api/organize-by-artist", None),
                ("post", "/api/fill-genres", None),
                ("post", "/api/fill-years", {}),
                ("post", "/api/unify-artist-genre", None),
                ("post", "/api/fix-artist-title", None),
                ("post", "/api/delete-tracks", {"track_ids": [1]}),
                ("post", "/api/duplicates/auto-clean", {"dry_run": False}),
                ("post", "/api/convert-tracks", {"format": "flac", "track_ids": [1]}),
            ):
                resp = getattr(self.client, method)(url, json=body) if body is not None else getattr(self.client, method)(url)
                data = resp.get_json()
                self.assertFalse(data.get("started"), (url, data))
                self.assertIn("isn't connected", data["error"], url)

    def test_jobs_routes(self):
        self.assertIsInstance(self.client.get("/api/jobs").get_json(), list)
        self.assertEqual(self.client.post("/api/jobs/nope/cancel").status_code, 404)
        # Nothing running -> cancel is a harmless no-op, not an error.
        self.assertFalse(self.client.post("/api/jobs/scan/cancel").get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
