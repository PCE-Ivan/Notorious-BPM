#!/usr/bin/env python3
"""Loudness measurement (loudness.py) and its routes. The parsing and gain
maths need nothing; the route tests make real audio with ffmpeg. Scratch
library only.

Run manually:

    python3 test_loudness.py
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import loudness

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)

SAMPLE = """\
[Parsed_ebur128_0 @ 0x1] t: 1.0 TARGET:-23 LUFS    M:  -8.3 S: -10.5     I:  -3.0 LUFS       LRA:   3.6 LU
[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:          -8.7 LUFS
    Threshold: -18.7 LUFS

  Loudness range:
    LRA:         3.6 LU

  True peak:
    Peak:        2.1 dBFS
[out#0/null @ 0x2] video:0KiB audio:25840KiB
"""


class ParseTest(unittest.TestCase):
    def test_reads_the_summary_not_the_running_lines(self):
        self.assertEqual(loudness.parse_ebur128(SAMPLE), (-8.7, 2.1))

    def test_silence_is_unmeasurable(self):
        with self.assertRaises(loudness.LoudnessError):
            loudness.parse_ebur128(SAMPLE.replace("-8.7 LUFS", "-inf LUFS"))

    def test_garbage_is_unmeasurable(self):
        with self.assertRaises(loudness.LoudnessError):
            loudness.parse_ebur128("nothing useful here")

    def test_gain_only_ever_turns_tracks_down(self):
        self.assertEqual(loudness.gain_db(-8.7), -5.3)      # loud: brought down to -14
        self.assertEqual(loudness.gain_db(-14.0), 0.0)
        self.assertEqual(loudness.gain_db(-20.0), 0.0)      # quiet: left alone, never boosted
        self.assertIsNone(loudness.gain_db(None))


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-loudness-")
        cls.music = os.path.join(cls.tmpdir, "music")
        os.makedirs(cls.music)

        def make(name, volume_db, **tags):
            meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
            subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                            "-af", f"volume={volume_db}dB", *meta, os.path.join(cls.music, name)], check=True)

        make("loud.mp3", 15, title="Loud")   # ffmpeg's sine starts at -18 dBFS: +15 dB is genuinely loud
        make("quiet.mp3", -15, title="Quiet")
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.music
        import app as app_module
        cls.m = app_module
        cls.client = app_module.app.test_client()
        cls.client.post("/api/rescan", json={})
        cls._wait("/api/scan-progress")
        time.sleep(0.3)
        cls._wait("/api/art/warm/progress")
        cls.ids = {t["title"]: t["id"] for t in cls.client.get("/api/tracks").get_json()["tracks"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @classmethod
    def _wait(cls, url, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            data = cls.client.get(url).get_json()
            if not data["running"]:
                return data
            time.sleep(0.05)
        raise AssertionError("never finished: " + url)

    def test_01_nothing_measured_means_no_gain(self):
        data = self.client.get(f"/api/loudness/{self.ids['Loud']}").get_json()
        self.assertEqual(data, {"lufs": None, "gain_db": None})

    def test_02_scan_measures_every_track_and_loud_ones_get_turned_down(self):
        started = self.client.post("/api/loudness/scan").get_json()
        self.assertEqual((started["started"], started["needed"]), (True, 2))
        done = self._wait("/api/loudness/progress")
        self.assertIsNone(done["error"], done)
        self.assertEqual(done["result"]["measured"], 2)

        loud = self.client.get(f"/api/loudness/{self.ids['Loud']}").get_json()
        quiet = self.client.get(f"/api/loudness/{self.ids['Quiet']}").get_json()
        self.assertGreater(loud["lufs"], quiet["lufs"] + 20)
        self.assertLess(loud["gain_db"], -3)          # a full-scale sine is well over the -14 target
        self.assertEqual(quiet["gain_db"], 0.0)       # well below the target: untouched
        status = self.client.get("/api/loudness/status").get_json()
        self.assertEqual((status["measured"], status["total"]), (2, 2))

    def test_03_second_scan_has_nothing_to_do(self):
        self.assertEqual(self.client.post("/api/loudness/scan").get_json(), {"started": False, "needed": 0})

    def test_04_reports_missing_ffmpeg(self):
        from unittest import mock
        with mock.patch.object(self.m, "_find_binary", return_value=None):
            self.assertEqual(self.client.post("/api/loudness/scan").get_json(), {"started": False, "error": "ffmpeg_missing"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
