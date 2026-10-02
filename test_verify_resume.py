#!/usr/bin/env python3
"""The whole-library "verify tags against audio" run: results are recorded per
track so it can be stopped and resumed, mismatches survive a restart, and a
dismissed one stays dismissed. fpcalc/AcoustID are mocked -- no network.
Scratch library only.

Run manually:

    python3 test_verify_resume.py
"""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

TMPDIR = tempfile.mkdtemp(prefix="jukebox-verifyresume-")
MUSIC = os.path.join(TMPDIR, "music")
os.makedirs(MUSIC)
os.environ["JUKEBOX_DB_PATH"] = os.path.join(TMPDIR, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(TMPDIR, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = MUSIC

import app as m  # noqa: E402  (after the environment is set up)

SONGS = [
    ("Falco", "Rock Me Amadeus"),
    ("Falco", "Der Kommissar"),
    ("A Flock of Seagulls", "I Ran"),        # this one's audio will turn out to be a different song
    ("Nena", "99 Luftballons"),
    ("Nena", "Irgendwie, Irgendwo, Irgendwann"),
]


class VerifyResumeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.get("/api/facets")  # builds the schema
        conn = sqlite3.connect(m.DB_PATH)
        for i, (artist, title) in enumerate(SONGS):
            name = f"{i}.mp3"
            with open(os.path.join(MUSIC, name), "wb") as f:
                f.write(b"x")
            conn.execute("INSERT INTO tracks (path, artist, title, ext, duration) VALUES (?,?,?,?,?)",
                         (name, artist, title, ".mp3", 200.0))
        conn.commit()
        conn.close()
        m.jukebox_config.update_config(lambda c: c.__setitem__("acoustidApiKey", "test-key"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    def setUp(self):
        self.calls = []
        patches = [
            mock.patch.object(m, "_find_binary", return_value="/bin/true"),
            mock.patch.object(m.time, "sleep", lambda s: None),
            mock.patch.object(m, "_fingerprint_lookup", side_effect=self.fake_lookup),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.cancel_after = None
        self.failing = False

    def fake_lookup(self, fpath, api_key, fpcalc_path, timeout=30):
        self.calls.append(os.path.basename(fpath))
        if self.failing:
            raise RuntimeError("network down")
        if self.cancel_after is not None and len(self.calls) >= self.cancel_after:
            m._verify_audio_job.cancel()
        idx = int(os.path.basename(fpath).split(".")[0])
        if SONGS[idx][1] == "I Ran":
            return "Falco", "Rock Me Amadeus", 0.97
        return SONGS[idx][0], SONGS[idx][1], 0.99

    def run_all(self):
        started = self.client.post("/api/verify-audio", json={}).get_json()
        self.assertTrue(started["started"], started)
        end = time.time() + 20
        while m._verify_audio_state["running"] and time.time() < end:
            time.sleep(0.02)
        self.assertFalse(m._verify_audio_state["running"])
        return m._verify_audio_state

    def clear_results(self):
        conn = sqlite3.connect(m.DB_PATH)
        conn.execute("DELETE FROM audio_verified")
        conn.commit()
        conn.close()

    def test_01_cancelled_run_resumes_with_only_the_remaining_tracks(self):
        self.cancel_after = 2
        state = self.run_all()
        self.assertTrue(state["cancelled"], state)
        self.assertEqual(len(self.calls), 2)
        status = self.client.get("/api/verify-audio/status").get_json()
        self.assertEqual((status["verified"], status["total"]), (2, 5))

        self.calls.clear()
        self.cancel_after = None
        state = self.run_all()
        self.assertIsNone(state["error"], state)
        self.assertEqual(len(self.calls), 3, self.calls)        # not 5: the first two were kept
        self.assertEqual(self.client.get("/api/verify-audio/status").get_json()["verified"], 5)

        self.calls.clear()
        self.run_all()
        self.assertEqual(self.calls, [])                          # nothing left to listen to

    def test_02_mismatches_persist_and_dismissal_sticks(self):
        results = self.client.get("/api/verify-audio/results").get_json()["mismatches"]
        self.assertEqual([(r["current_title"], r["found_title"]) for r in results], [("I Ran", "Rock Me Amadeus")])
        self.assertEqual(self.client.get("/api/verify-audio/status").get_json()["mismatches"], 1)

        track_id = results[0]["id"]
        self.assertTrue(self.client.post("/api/verify-audio/dismiss", json={"track_id": track_id}).get_json()["ok"])
        self.assertEqual(self.client.get("/api/verify-audio/results").get_json()["mismatches"], [])

    def test_03_a_fixed_tag_drops_out_of_the_results(self):
        conn = sqlite3.connect(m.DB_PATH)
        conn.execute("UPDATE audio_verified SET dismissed = 0")
        conn.commit()
        self.assertEqual(len(self.client.get("/api/verify-audio/results").get_json()["mismatches"]), 1)
        conn.execute("UPDATE tracks SET artist='Falco', title='Rock Me Amadeus' WHERE title='I Ran'")
        conn.commit()
        conn.close()
        self.assertEqual(self.client.get("/api/verify-audio/results").get_json()["mismatches"], [])

    def test_04_errors_are_retried_and_a_dead_network_stops_the_run(self):
        self.clear_results()
        self.failing = True
        with mock.patch.object(m, "_VERIFY_MAX_CONSECUTIVE_ERRORS", 3):       # only 5 tracks here
            state = self.run_all()
        self.assertIn("isn't answering", state["error"])
        self.assertEqual(len(self.calls), 3)                                    # gave up instead of hammering
        self.assertEqual(self.client.get("/api/verify-audio/status").get_json()["verified"], 0)

        self.calls.clear()
        self.failing = False
        state = self.run_all()
        self.assertIsNone(state["error"], state)
        self.assertEqual(len(self.calls), 5)                                    # errored ones were retried

    def test_05_selected_tracks_are_always_rechecked(self):
        ids = [r[0] for r in sqlite3.connect(m.DB_PATH).execute("SELECT id FROM tracks LIMIT 2")]
        self.client.post("/api/verify-audio", json={"track_ids": ids})
        end = time.time() + 10
        while m._verify_audio_state["running"] and time.time() < end:
            time.sleep(0.02)
        self.assertEqual(len(self.calls), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
