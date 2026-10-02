#!/usr/bin/env python3
"""Importing dropped / picked music (importer.py and /api/import/*). Real tiny
audio files made with ffmpeg; scratch library only.

Run manually:

    python3 test_importer.py
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest

TMPDIR = tempfile.mkdtemp(prefix="jukebox-import-")
MUSIC = os.path.join(TMPDIR, "music")
INBOX = os.path.join(TMPDIR, "inbox")
os.makedirs(MUSIC)
os.makedirs(INBOX)
os.environ["JUKEBOX_DB_PATH"] = os.path.join(TMPDIR, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(TMPDIR, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = MUSIC

import app as m  # noqa: E402
import importer  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)


def make(path, artist=None, title=None, freq=440, seconds=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = []
    if artist:
        meta += ["-metadata", f"artist={artist}"]
    if title:
        meta += ["-metadata", f"title={title}"]
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    *meta, path], check=True)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class ImporterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.get("/api/facets")
        # one track already in the library
        make(os.path.join(MUSIC, "Queen", "Bohemian Rhapsody.mp3"), "Queen", "Bohemian Rhapsody", 300)
        cls.client.post("/api/rescan", json={})
        cls.wait("/api/scan-progress")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    @classmethod
    def wait(cls, url, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            data = cls.client.get(url).get_json()
            if not data["running"]:
                return data
            time.sleep(0.05)
        raise AssertionError("never finished: " + url)

    def test_01_collects_audio_from_files_and_folders_only(self):
        make(os.path.join(INBOX, "album", "01 a.mp3"), "X", "A")
        make(os.path.join(INBOX, "album", "sub", "02 b.mp3"), "X", "B")
        with open(os.path.join(INBOX, "album", "cover.jpg"), "wb") as f:
            f.write(b"jpg")
        with open(os.path.join(INBOX, "album", "._01 a.mp3"), "wb") as f:
            f.write(b"appledouble")
        os.makedirs(os.path.join(INBOX, "album", ".hidden"))
        make(os.path.join(INBOX, "album", ".hidden", "03 c.mp3"), "X", "C")
        make(os.path.join(INBOX, "single.mp3"), "Y", "S")
        files = importer.collect_audio_files([os.path.join(INBOX, "album"), os.path.join(INBOX, "single.mp3"),
                                              os.path.join(INBOX, "album", "01 a.mp3"), "/no/such/file.mp3"])
        self.assertEqual([os.path.relpath(f, INBOX) for f in files],
                         ["album/01 a.mp3", "album/sub/02 b.mp3", "single.mp3"])   # no sidecar, hidden dir, cover or duplicate

    def test_02_import_copies_into_artist_folders_skips_duplicates_and_rescans(self):
        shutil.rmtree(INBOX)
        os.makedirs(INBOX)
        make(os.path.join(INBOX, "fresh one.mp3"), "Nena", "99 Luftballons", 500)
        make(os.path.join(INBOX, "queen copy.mp3"), "Queen", "Bohemian Rhapsody (Remastered)", 310)  # same song, other edition
        make(os.path.join(INBOX, "untagged.mp3"), None, None, 700)
        make(os.path.join(INBOX, "zz fresh again.mp3"), "Nena", "99 Luftballons", 500)            # dropped twice in one go

        started = self.client.post("/api/import/files", json={"paths": [INBOX]}).get_json()
        self.assertTrue(started["started"], started)
        done = self.wait("/api/import/progress")
        self.assertIsNone(done["error"], done)
        r = done["result"]
        self.assertEqual(sorted(r["copied"]), [os.path.join("Nena", "fresh one.mp3"), os.path.join("Unknown Artist", "untagged.mp3")])
        self.assertEqual(sorted((d["artist"], d["title"]) for d in r["duplicates"]),
                         [("Nena", "99 Luftballons"), ("Queen", "Bohemian Rhapsody (Remastered)")])
        for d in r["duplicates"]:
            self.assertIn("library_bitrate", d)
        self.assertEqual(r["errors"], [])

        # originals untouched, copies present
        self.assertTrue(os.path.exists(os.path.join(INBOX, "fresh one.mp3")))
        self.assertTrue(os.path.exists(os.path.join(MUSIC, "Nena", "fresh one.mp3")))
        self.assertFalse([f for _r, _d, fs in os.walk(MUSIC) for f in fs if f.endswith(".part")])

        # the chained rescan indexed them
        time.sleep(0.2)
        self.wait("/api/scan-progress")
        titles = {t["title"] for t in self.client.get("/api/tracks").get_json()["tracks"]}
        self.assertIn("99 Luftballons", titles)

        # importing the same inbox again adds nothing
        self.client.post("/api/import/files", json={"paths": [INBOX]})
        again = self.wait("/api/import/progress")["result"]
        self.assertEqual(again["copied"], [])
        self.assertEqual(sorted((d["artist"], d["title"]) for d in again["duplicates"]),
                         [("Nena", "99 Luftballons"), ("Nena", "99 Luftballons"), ("Queen", "Bohemian Rhapsody (Remastered)")])
        self.assertEqual(again["in_library"], 1)   # the untagged one: same name and size as the copy made before
        self.wait("/api/scan-progress")

    def test_02b_the_same_untagged_file_dropped_again_is_not_copied_twice(self):
        self.client.post("/api/import/files", json={"paths": [os.path.join(INBOX, "untagged.mp3")]})
        r = self.wait("/api/import/progress")["result"]
        self.assertEqual((r["copied"], r["in_library"]), ([], 1))
        self.wait("/api/scan-progress")

    def test_03_files_already_in_the_music_folder_are_left_alone(self):
        self.client.post("/api/import/files", json={"paths": [os.path.join(MUSIC, "Queen")]})
        r = self.wait("/api/import/progress")["result"]
        self.assertEqual((r["copied"], r["in_library"]), ([], 1))

    def test_04_name_collisions_get_a_suffix_not_an_overwrite(self):
        other = os.path.join(TMPDIR, "other")
        make(os.path.join(other, "fresh one.mp3"), "Nena", "A Different Song", 900, seconds=2)   # a different file with the same name
        self.client.post("/api/import/files", json={"paths": [other]})
        r = self.wait("/api/import/progress")["result"]
        self.assertEqual(r["copied"], [os.path.join("Nena", "fresh one (2).mp3")])
        self.wait("/api/scan-progress")

    def test_05_bad_requests(self):
        self.assertEqual(self.client.post("/api/import/files", json={"paths": "nope"}).status_code, 400)
        self.assertFalse(self.client.post("/api/import/files", json={"paths": []}).get_json()["started"])

    def test_06_unreachable_music_folder_refuses(self):
        from unittest import mock
        with mock.patch.object(m, "MUSIC_DIR", "/Volumes/DefinitelyNotMountedDrive-12345/Music"):
            data = self.client.post("/api/import/files", json={"paths": [INBOX]}).get_json()
        self.assertFalse(data["started"])
        self.assertIn("isn't connected", data["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
