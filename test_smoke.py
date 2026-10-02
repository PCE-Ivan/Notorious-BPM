#!/usr/bin/env python3
"""Thin smoke test: boots the real Flask app against a throwaway scratch DB
and config (same env-var override pattern used for manual testing throughout
development -- see JUKEBOX_DB_PATH/JUKEBOX_CONFIG_PATH/JUKEBOX_MUSIC_DIR in
config.py), then hits the main routes and checks they come back sane.

Not a full test suite -- it exists to catch the specific class of mistake
that shipped broken this project's history so far: code that imports fine
but was never actually run before being packaged (a stale build shipped
against new code; a route that only breaks once actually called). Run it
manually before every build_macos.sh:

    python3 test_smoke.py

Never touches the real library.db/config.json -- everything here runs
against a fresh temp directory, deleted again at the end.
"""
import json
import os
import shutil
import tempfile
import unittest


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-smoketest-")
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.tmpdir  # empty, valid dir -- no real scan needed for these checks

        import app as app_module
        cls.app_module = app_module
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_index_serves(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_layout_mode_persists_and_rejects_invalid(self):
        resp = self.client.post("/api/layout-mode", json={"layoutMode": "row-density"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/layout-mode")
        self.assertEqual(resp.get_json()["layoutMode"], "row-density")

        resp = self.client.post("/api/layout-mode", json={"layoutMode": "not-a-real-layout"})
        self.assertEqual(resp.status_code, 400)
        # Rejected value must not have overwritten the last valid one.
        resp = self.client.get("/api/layout-mode")
        self.assertEqual(resp.get_json()["layoutMode"], "row-density")

        self.client.post("/api/layout-mode", json={"layoutMode": "classic"})

    def test_instance_endpoint_identifies_the_app_and_its_library(self):
        data = self.client.get("/api/instance").get_json()
        self.assertEqual(data["app"], "notorious-bpm")
        self.assertEqual(data["library"], self.app_module.DB_PATH)

    def test_facets_on_empty_library(self):
        resp = self.client.get("/api/facets")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        for key in ("genres", "decades", "artists", "languages", "total"):
            self.assertIn(key, data)
        self.assertEqual(data["total"], 0)

    def test_tracks_on_empty_library(self):
        resp = self.client.get("/api/tracks")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["tracks"], [])

    def test_ipod_detect_with_no_device(self):
        # No real iPod is expected to be connected in CI/dev -- this just
        # confirms the route itself doesn't error out, and that
        # find_ipod() returning None is reported as found:false rather
        # than a 500.
        resp = self.client.get("/api/ipod/detect")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.get_json()["found"], (False, True))

    def test_scan_progress_route(self):
        resp = self.client.get("/api/scan-progress")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("running", resp.get_json())

    def test_last_scan_route(self):
        resp = self.client.get("/api/last-scan")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("last_scan_at", data)
        self.assertIn("seconds_ago", data)

    def test_duplicates_route_on_empty_library(self):
        resp = self.client.get("/api/duplicates")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["total_groups"], 0)

    def test_stats_route(self):
        resp = self.client.get("/api/stats")
        self.assertEqual(resp.status_code, 200)

    def test_rescan_starts_and_reports_progress(self):
        resp = self.client.post("/api/rescan", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["started"])
        # Give the background thread a moment, then confirm the progress
        # route reflects a completed (or in-progress) scan rather than
        # erroring -- this is exactly the kind of route that only ever
        # gets exercised by a human clicking "Rescan" otherwise.
        import time
        for _ in range(50):
            status = self.client.get("/api/scan-progress").get_json()
            if not status["running"]:
                break
            time.sleep(0.1)
        self.assertFalse(status["running"])
        self.assertIsNone(status["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
