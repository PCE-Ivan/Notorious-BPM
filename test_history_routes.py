#!/usr/bin/env python3
"""End-to-end: edit tags through the real routes, see the change in the
history, undo it as a background job, confirm files and index are back.
Scratch library only (env set before app is imported).

Run manually:

    python3 test_history_routes.py
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class HistoryRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-historyroutes-")
        cls.music = os.path.join(cls.tmpdir, "music")
        os.makedirs(cls.music)
        for name in ("one.mp3", "two.mp3"):
            subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.3",
                            "-metadata", "artist=Band", "-metadata", f"title={name}", "-metadata", "genre=Rock",
                            os.path.join(cls.music, name)], check=True)
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.music
        import app as app_module
        cls.app_module = app_module
        cls.client = app_module.app.test_client()
        cls.client.post("/api/rescan", json={})
        cls._wait("/api/scan-progress")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @classmethod
    def _wait(cls, url, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if not cls.client.get(url).get_json()["running"]:
                return cls.client.get(url).get_json()
            time.sleep(0.05)
        raise AssertionError("job never finished: " + url)

    def test_edit_then_undo(self):
        tracks = self.client.get("/api/tracks").get_json()["tracks"]
        self.assertEqual(len(tracks), 2)
        for t in tracks:
            r = self.client.post(f"/api/tags/{t['id']}", json={"field": "genre", "value": "Jazz", "batch": "b-1"})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        history = self.client.get("/api/history").get_json()
        self.assertEqual(len(history), 1, "both edits share one batch -> one operation")
        self.assertEqual((history[0]["item_count"], history[0]["undone"]), (2, 0))

        import tagio
        for t in tracks:
            self.assertEqual(tagio.read_tag(os.path.join(self.music, self._path(t["id"])), "genre"), "Jazz")

        self.assertTrue(self.client.post(f"/api/history/{history[0]['id']}/undo").get_json()["started"])
        state = self._wait("/api/history/progress")
        self.assertIsNone(state["error"], state)
        self.assertEqual(state["result"]["restored"], 2)

        for t in tracks:
            self.assertEqual(tagio.read_tag(os.path.join(self.music, self._path(t["id"])), "genre"), "Rock")
        self.assertEqual(self.client.get("/api/history").get_json()[0]["undone"], 1)
        genres = {t["primary_genre"] for t in self.client.get("/api/tracks").get_json()["tracks"]}
        self.assertEqual(genres, {"Rock"})

    def _path(self, track_id):
        db = self.app_module.get_db
        with self.app_module.app.app_context():
            return db().execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()["path"]


if __name__ == "__main__":
    unittest.main()
