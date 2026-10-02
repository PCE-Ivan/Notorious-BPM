#!/usr/bin/env python3
"""Route-level test of the library health check + repair against a scratch
library (env vars set before app is imported, so nothing real is touched).

Run manually:

    python3 test_health_routes.py
"""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest


class HealthRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-healthroutes-")
        cls.music = os.path.join(cls.tmpdir, "music")
        os.makedirs(cls.music)
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.music
        import app as app_module
        cls.app_module = app_module
        cls.client = app_module.app.test_client()
        cls.client.get("/api/facets")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _wait_idle(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if not self.client.get("/api/health/progress").get_json()["running"]:
                return
            time.sleep(0.05)
        self.fail("health check never finished")

    def test_check_then_repair_roundtrip(self):
        m = self.app_module
        conn = sqlite3.connect(m.DB_PATH)
        for n in range(9):
            open(os.path.join(self.music, f"ok{n}.mp3"), "wb").write(b"x")
            conn.execute("INSERT INTO tracks (path, artist, title) VALUES (?,?,?)", (f"ok{n}.mp3", "A", f"t{n}"))
        conn.execute("INSERT INTO tracks (path, artist, title) VALUES ('ghost.mp3', 'A', 'ghost')")
        conn.commit(); conn.close()
        open(os.path.join(m.ART_CACHE_DIR, "424242.jpg"), "wb").close()

        self.assertTrue(self.client.post("/api/health/check").get_json()["started"])
        self._wait_idle()
        state = self.client.get("/api/health/progress").get_json()
        self.assertIsNone(state["error"], state)
        ids = {i["id"] for i in state["result"]["issues"]}
        self.assertIn("missing_files", ids)
        self.assertIn("art_orphans", ids)
        self.assertEqual(self.client.get("/api/health/last").get_json()["result"]["tracks"], 10)

        resp = self.client.post("/api/health/repair", json={"repairs": ["remove_missing_tracks", "delete_orphan_art", "bogus"]})
        body = resp.get_json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["results"], {"remove_missing_tracks": 1, "delete_orphan_art": 1})  # unknown ids ignored

        self.client.post("/api/health/check")
        self._wait_idle()
        ids = {i["id"] for i in self.client.get("/api/health/progress").get_json()["result"]["issues"]}
        self.assertNotIn("missing_files", ids)
        self.assertNotIn("art_orphans", ids)

    def test_repair_requires_a_selection(self):
        self.assertFalse(self.client.post("/api/health/repair", json={"repairs": []}).get_json()["ok"])

    def test_repair_refused_while_a_job_runs(self):
        m = self.app_module
        m._scan_state["running"] = True
        try:
            body = self.client.post("/api/health/repair", json={"repairs": ["delete_orphan_art"]}).get_json()
        finally:
            m._scan_state["running"] = False
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
