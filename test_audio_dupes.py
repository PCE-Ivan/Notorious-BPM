#!/usr/bin/env python3
"""Duplicate detection by sound (audio_dupes.py + its routes in app.py).

Two layers:
  * the matcher itself, on synthetic fingerprints -- no tools needed;
  * the whole feature through the Flask routes, on real audio that is
    synthesised and encoded with ffmpeg and fingerprinted with fpcalc
    (skipped where either is missing). Scratch library only.

Run manually:

    python3 test_audio_dupes.py
"""
import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import wave

import audio_dupes as ad

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)
FPCALC = shutil.which("fpcalc") or ("/opt/homebrew/bin/fpcalc" if os.path.exists("/opt/homebrew/bin/fpcalc") else None)


def noisy_copy(fp, rate, rnd, shift=0):
    """What a different encode of the same audio looks like: every bit has a
    `rate` chance of flipping, and the start is offset by `shift` frames."""
    out = []
    for v in fp:
        for bit in range(32):
            if rnd.random() < rate:
                v ^= 1 << bit
        out.append(v)
    return ad.pack(([rnd.getrandbits(32) for _ in range(shift)] if shift > 0 else []) + out[(-shift if shift < 0 else 0):])


class MatcherTest(unittest.TestCase):
    def setUp(self):
        self.rnd = random.Random(7)

    def random_fp(self, n=900):
        return ad.pack(self.rnd.getrandbits(32) for _ in range(n))

    def test_blob_round_trip(self):
        fp = self.random_fp(50)
        self.assertEqual(list(ad.from_blob(ad.to_blob(fp))), list(fp))

    def test_ber_of_identical_and_unrelated(self):
        a, b = self.random_fp(), self.random_fp()
        self.assertEqual(ad.ber_at(a, a, 0), (0.0, 900))
        ber, n = ad.ber_at(a, b, 0)
        self.assertAlmostEqual(ber, 0.5, delta=0.03)

    def test_finds_noisy_copy_at_an_unknown_offset(self):
        a = self.random_fp()
        for shift in (0, 5, 23, -17):
            b = noisy_copy(a, 0.07, self.rnd, shift=shift)
            pairs = ad.find_pairs([(1, 200.0, a), (2, 200.4, b)])
            self.assertEqual([(p[0], p[1]) for p in pairs], [(1, 2)], f"shift {shift}")
            self.assertLess(pairs[0][2], 0.15)

    def test_unrelated_tracks_never_pair(self):
        entries = [(i, 200.0 + (i % 3), self.random_fp()) for i in range(1, 25)]
        self.assertEqual(ad.find_pairs(entries), [])

    def test_different_length_is_a_different_recording(self):
        a = self.random_fp()
        b = noisy_copy(a, 0.05, self.rnd)
        self.assertEqual(ad.find_pairs([(1, 200.0, a), (2, 215.0, b)]), [])

    def test_neighbouring_duration_buckets_still_pair(self):
        a = self.random_fp()
        b = noisy_copy(a, 0.05, self.rnd)
        # 199.9 and 201.2 fall either side of a bucket edge
        self.assertEqual(len(ad.find_pairs([(1, 199.9, a), (2, 201.2, b)])), 1)

    def test_a_heavily_different_master_is_rejected(self):
        a = self.random_fp()
        b = noisy_copy(a, 0.30, self.rnd)  # ~0.30 BER: past the threshold
        self.assertEqual(ad.find_pairs([(1, 200.0, a), (2, 200.0, b)]), [])

    def test_groups_chain_and_report_the_weakest_link(self):
        groups = ad.group_pairs([(1, 2, 0.05), (2, 3, 0.12), (7, 8, 0.02)])
        self.assertEqual(groups[0]["ids"], [1, 2, 3])
        self.assertEqual(groups[0]["ber"], 0.12)
        self.assertEqual(groups[1]["ids"], [7, 8])

    def test_progress_callback_can_cancel(self):
        entries = [(i, 200.0, self.random_fp(50)) for i in range(1, 60)]

        class Stop(Exception):
            pass

        def progress(done, total):
            raise Stop()

        with self.assertRaises(Stop):
            ad.find_pairs(entries, progress=progress)


def synth(path, seed, seconds=24, rate=22050):
    """A made-up tune (random notes with harmonics and noise-burst drums) --
    pure tones fingerprint to a constant, which is useless for testing."""
    rnd = random.Random(seed)
    scale = [0, 2, 3, 5, 7, 8, 10, 12, 14, 15, 17, 19]
    root = rnd.choice([110.0, 123.47, 130.81, 146.83])
    n = int(seconds * rate)
    buf = [0.0] * n
    t = 0.0
    while t < seconds:
        beat = rnd.choice([0.2, 0.25, 0.3, 0.4, 0.5])
        f = root * 2 ** (rnd.choice(scale) / 12.0) * rnd.choice([1, 2, 2, 4])
        amp = rnd.uniform(0.15, 0.35)
        start = int(t * rate)
        for i in range(int(min(beat * 1.6, seconds - t) * rate)):
            x = i / rate
            env = math.exp(-x * 5.0) * min(1.0, i / 200.0)
            v = math.sin(2 * math.pi * f * x) + 0.5 * math.sin(4 * math.pi * f * x) + 0.25 * math.sin(6 * math.pi * f * x)
            if start + i < n:
                buf[start + i] += amp * env * v
        if rnd.random() < 0.6:
            for i in range(int(0.04 * rate)):
                if start + i < n:
                    buf[start + i] += rnd.uniform(-1, 1) * 0.25 * math.exp(-i / (0.01 * rate))
        t += beat
    peak = max(abs(v) for v in buf) or 1.0
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v / peak * 0.9)) * 32767)) for v in buf))


@unittest.skipUnless(FFMPEG and FPCALC, "needs ffmpeg and fpcalc")
class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-audiodupes-")
        cls.music = os.path.join(cls.tmpdir, "music")
        os.makedirs(cls.music)
        for seed in (1, 2, 3):
            synth(os.path.join(cls.tmpdir, f"s{seed}.wav"), seed)

        def encode(src, name, *args, **tags):
            meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
            subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-i", os.path.join(cls.tmpdir, src), *args, *meta,
                            os.path.join(cls.music, name)], check=True)

        # Song 1 exists three times under unrelated names and tags, in different encodings.
        encode("s1.wav", "alpha.mp3", "-b:a", "128k", artist="One", title="Original")
        encode("s1.wav", "something else.flac", "-ss", "0.6", artist="Another", title="Different Name")
        encode("s1.wav", "zz.m4a", "-b:a", "96k", artist="Third", title="Third Tag")
        encode("s2.wav", "beta.mp3", "-b:a", "128k", artist="Two", title="Unrelated Two")
        encode("s3.wav", "gamma.mp3", "-b:a", "128k", artist="Three", title="Unrelated Three")
        # Too short to fingerprint: remembered as such, not retried on every run.
        subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=0.3",
                        "-metadata", "title=Blip", os.path.join(cls.music, "blip.mp3")], check=True)

        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.music
        import app as app_module
        cls.m = app_module
        cls.client = app_module.app.test_client()
        cls.client.post("/api/rescan", json={})
        cls._wait_for("/api/scan-progress")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @classmethod
    def _wait_for(cls, url, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            data = cls.client.get(url).get_json()
            if not data["running"]:
                return data
            time.sleep(0.05)
        raise AssertionError("never finished: " + url)

    def _scan(self):
        started = self.client.post("/api/audio-dupes/scan").get_json()
        self.assertTrue(started["started"], started)
        done = self._wait_for("/api/audio-dupes/progress")
        self.assertIsNone(done["error"], done)
        return started, done

    def test_01_finds_the_same_song_under_different_names_and_formats(self):
        started, done = self._scan()
        self.assertEqual(started["needed"], 6)
        self.assertEqual(done["result"]["groups"], 1)
        self.assertEqual(done["result"]["tracks"], 3)
        self.assertEqual(done["result"]["failed"], 1)  # the too-short file

        data = self.client.get("/api/audio-dupes/groups").get_json()
        self.assertEqual(data["total_groups"], 1)
        group = data["groups"][0]
        self.assertEqual({t["title"] for t in group["tracks"]}, {"Original", "Different Name", "Third Tag"})
        self.assertIn("recording", group["match"])
        for t in group["tracks"]:
            self.assertTrue(t["size"] and t["kbps"], t)

    def test_02_second_run_reuses_the_cached_fingerprints(self):
        started, done = self._scan()
        self.assertEqual(started["needed"], 0)
        self.assertEqual(done["result"]["groups"], 1)
        status = self.client.get("/api/audio-dupes/status").get_json()
        self.assertEqual((status["fingerprinted"], status["total"]), (6, 6))

    def test_03_dismissal_is_remembered_and_resettable(self):
        group = self.client.get("/api/audio-dupes/groups").get_json()["groups"][0]
        ids = [t["id"] for t in group["tracks"]]
        self.assertTrue(self.client.post("/api/duplicates/dismiss", json={"track_ids": ids}).get_json()["ok"])
        self.assertEqual(self.client.get("/api/audio-dupes/groups").get_json()["total_groups"], 0)
        self._scan()  # a fresh comparison doesn't resurrect it
        self.assertEqual(self.client.get("/api/audio-dupes/groups").get_json()["total_groups"], 0)
        cleared = self.client.post("/api/duplicates/dismissed/clear").get_json()
        self.assertEqual(cleared["cleared"], 3)
        self.assertEqual(self.client.get("/api/audio-dupes/groups").get_json()["total_groups"], 1)

    def test_04_deleting_copies_shrinks_the_group(self):
        group = self.client.get("/api/audio-dupes/groups").get_json()["groups"][0]
        victim = group["tracks"][0]["id"]
        self.client.post("/api/delete-tracks", json={"track_ids": [victim]})
        self._wait_for("/api/delete-tracks/progress")
        data = self.client.get("/api/audio-dupes/groups").get_json()
        self.assertEqual(data["total_groups"], 1)
        self.assertEqual(len(data["groups"][0]["tracks"]), 2)
        self.assertNotIn(victim, [t["id"] for t in data["groups"][0]["tracks"]])
        # the deleted track's cached fingerprint went with it (foreign key cascade)
        status = self.client.get("/api/audio-dupes/status").get_json()
        self.assertEqual(status["fingerprinted"], 5)

    def test_05_dismissing_validates_input(self):
        self.assertEqual(self.client.post("/api/duplicates/dismiss", json={"track_ids": [1]}).status_code, 400)

    def test_06_cancel_stops_the_job_cleanly(self):
        # Hold the job at its first progress tick, cancel, and make sure it ends as cancelled.
        import threading
        gate = threading.Event()
        real = ad.compute_fingerprint

        def slow(*a, **k):
            gate.wait(5)
            return real(*a, **k)

        self.m.audio_dupes.compute_fingerprint = slow
        try:
            self.m._invalidate_dup_plan_cache()
            conn = self.m.sqlite3.connect(self.m.DB_PATH)
            conn.execute("DELETE FROM audio_fingerprints")
            conn.commit()
            conn.close()
            self.assertTrue(self.client.post("/api/audio-dupes/scan").get_json()["started"])
            self.assertTrue(self.client.post("/api/jobs/audio_dupes/cancel").get_json()["ok"])
            gate.set()
            done = self._wait_for("/api/audio-dupes/progress")
            self.assertTrue(done["cancelled"], done)
        finally:
            self.m.audio_dupes.compute_fingerprint = real
            gate.set()

    def test_07_scan_reports_missing_fpcalc(self):
        from unittest import mock
        with mock.patch.object(self.m, "_find_binary", return_value=None):
            data = self.client.post("/api/audio-dupes/scan").get_json()
        self.assertEqual(data, {"started": False, "error": "fpcalc_missing"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
