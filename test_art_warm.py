#!/usr/bin/env python3
"""The background thumbnail pass: after a scan, every track ends up with a
ready thumbnail or a "no art" marker, and has_art matches reality.
Scratch library only; real tiny audio files made with ffmpeg + Pillow.

Run manually:

    python3 test_art_warm.py
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class ArtWarmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-artwarm-")
        cls.music = os.path.join(cls.tmpdir, "music")
        os.makedirs(cls.music)
        from PIL import Image
        cover = os.path.join(cls.tmpdir, "cover.jpg")
        Image.new("RGB", (300, 300), (200, 40, 40)).save(cover, "JPEG")
        subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.3", "-i", cover,
                        "-map", "0", "-map", "1", "-c:a", "libmp3lame", "-c:v", "copy", "-id3v2_version", "3",
                        "-metadata", "artist=A", "-metadata", "title=WithArt", os.path.join(cls.music, "with_art.mp3")],
                       check=True)
        subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.3",
                        "-metadata", "artist=A", "-metadata", "title=NoArt", os.path.join(cls.music, "no_art.mp3")],
                       check=True)
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.music
        import app as app_module
        cls.m = app_module
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _wait(self, url, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            data = self.client.get(url).get_json()
            if not data["running"]:
                return data
            time.sleep(0.05)
        self.fail("never finished: " + url)

    def test_scan_triggers_thumbnails_and_truthful_has_art(self):
        self.client.post("/api/rescan", json={})
        self._wait("/api/scan-progress")
        # the scan chains the warm job; give it a moment to start, then wait it out
        time.sleep(0.3)
        warm = self._wait("/api/art/warm/progress")
        self.assertIsNone(warm["error"], warm)

        by_title = {t["title"]: t for t in self.client.get("/api/tracks").get_json()["tracks"]}
        self.assertEqual(by_title["WithArt"]["has_art"], 1)
        self.assertEqual(by_title["NoArt"]["has_art"], 0)
        art_dir = self.m.ART_CACHE_DIR
        self.assertTrue(os.path.exists(os.path.join(art_dir, f"{by_title['WithArt']['id']}.thumb.jpg")))
        self.assertTrue(os.path.exists(os.path.join(art_dir, f"{by_title['NoArt']['id']}.none")))

        # nothing left to do -> asking again starts nothing
        self.assertEqual(self.client.post("/api/art/warm").get_json(), {"started": False, "needed": 0})

    def test_a_library_switch_does_not_wait_on_housekeeping(self):
        import threading
        gate = threading.Event()
        self.m._art_warm_job.start(gate.wait)
        try:
            self.assertTrue(self.m._art_warm_job.running)
            self.m._stop_housekeeping_jobs(wait=0.2)
            self.assertTrue(self.m._art_warm_job._cancel.is_set())
        finally:
            gate.set()
            time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
