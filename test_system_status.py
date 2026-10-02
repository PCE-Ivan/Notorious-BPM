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

    def test_describe_exception_corrupt_database(self):
        kind, _ = system_status.describe_exception(
            sqlite3.DatabaseError("database disk image is malformed"), self.db, self.music)
        self.assertEqual(kind, "library_corrupt")


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

    def test_jobs_routes(self):
        self.assertIsInstance(self.client.get("/api/jobs").get_json(), list)
        self.assertEqual(self.client.post("/api/jobs/nope/cancel").status_code, 404)
        # Nothing running -> cancel is a harmless no-op, not an error.
        self.assertFalse(self.client.post("/api/jobs/scan/cancel").get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
