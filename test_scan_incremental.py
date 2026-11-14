#!/usr/bin/env python3
"""Rescan skips unchanged files and recognises renamed ones. Real tiny audio
files made with ffmpeg; scratch library only.

Run manually:

    python3 test_scan_incremental.py
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

TMPDIR = tempfile.mkdtemp(prefix="jukebox-scan-")
MUSIC = os.path.join(TMPDIR, "music")
os.makedirs(MUSIC)
os.environ["JUKEBOX_DB_PATH"] = os.path.join(TMPDIR, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(TMPDIR, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = MUSIC

import scan_library  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)


def make(rel, artist, title, freq=440, seconds=1):
    path = os.path.join(MUSIC, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-metadata", f"artist={artist}", "-metadata", f"title={title}", path], check=True)


def db():
    conn = sqlite3.connect(os.environ["JUKEBOX_DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def track(path):
    conn = db()
    row = conn.execute("SELECT * FROM tracks WHERE path=?", (path,)).fetchone()
    conn.close()
    return row


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class IncrementalScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make("Queen/One.mp3", "Queen", "One", 300, 1)
        make("Queen/Two.mp3", "Queen", "Two", 400, 2)
        make("ABBA/Three.mp3", "ABBA", "Three", 500, 3)
        cls.first = scan_library.scan()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    def test_1_first_scan_reads_everything_and_remembers_the_files(self):
        self.assertEqual((self.first["inserted"], self.first["unchanged"]), (3, 0))
        row = track("Queen/One.mp3")
        self.assertEqual(row["file_size"], os.path.getsize(os.path.join(MUSIC, "Queen/One.mp3")))
        self.assertIsNotNone(row["file_mtime"])

    def test_2_second_scan_skips_unchanged_files(self):
        r = scan_library.scan()
        self.assertEqual((r["inserted"], r["updated"], r["unchanged"], r["removed"]), (0, 0, 3, 0))

    def test_3_a_changed_file_is_read_again_and_full_reads_all(self):
        make("Queen/One.mp3", "Queen", "One (Retagged)", 300, 1)
        r = scan_library.scan()
        self.assertEqual((r["updated"], r["unchanged"]), (1, 2))
        self.assertEqual(track("Queen/One.mp3")["title"], "One (Retagged)")
        r = scan_library.scan(full=True)
        self.assertEqual((r["updated"], r["unchanged"]), (3, 0))

    def test_4_a_renamed_file_keeps_its_row_and_rating(self):
        before = track("Queen/Two.mp3")
        conn = db()
        conn.execute("INSERT INTO ratings (track_id, rating) VALUES (?, 4)", (before["id"],))
        conn.commit()
        conn.close()
        os.makedirs(os.path.join(MUSIC, "Queen (UK)"))
        os.rename(os.path.join(MUSIC, "Queen/Two.mp3"), os.path.join(MUSIC, "Queen (UK)/02 - Two.mp3"))
        r = scan_library.scan()
        self.assertEqual((r["moved"], r["inserted"], r["removed"]), (1, 0, 0))
        after = track("Queen (UK)/02 - Two.mp3")
        self.assertEqual(after["id"], before["id"])
        self.assertIsNone(track("Queen/Two.mp3"))
        conn = db()
        self.assertEqual(conn.execute("SELECT rating FROM ratings WHERE track_id=?", (after["id"],)).fetchone()[0], 4)
        conn.close()

    def test_5_a_deleted_file_is_still_removed_and_a_new_one_added(self):
        os.remove(os.path.join(MUSIC, "ABBA/Three.mp3"))
        make("ABBA/Four.mp3", "ABBA", "Four", 600, 4)   # different length: not "Three, renamed"
        r = scan_library.scan()
        self.assertEqual((r["moved"], r["inserted"], r["removed"]), (0, 1, 1))

    def test_6_ambiguous_matches_are_not_guessed(self):
        index = scan_library._MovedIndex([
            {"id": 1, "path": "a.mp3", "artist": "X", "album": None, "title": "Song", "duration": 10.0, "ext": ".mp3", "file_size": 5},
            {"id": 2, "path": "b.mp3", "artist": "X", "album": None, "title": "Song", "duration": 10.0, "ext": ".mp3", "file_size": 5},
        ])
        fields = {"ext": ".mp3", "duration": 10.0, "title": "Song", "artist": "X", "album": None}
        self.assertIsNone(index.claim(fields, 5))
        single = scan_library._MovedIndex([
            {"id": 1, "path": "a.mp3", "artist": "X", "album": None, "title": "Song", "duration": 10.0, "ext": ".mp3", "file_size": 5}])
        self.assertEqual(single.claim(fields, 999)["id"], 1)      # retagged size, same tags
        self.assertIsNone(single.claim(fields, 5))                # already claimed
        self.assertIsNone(scan_library._MovedIndex([
            {"id": 1, "path": "a.mp3", "artist": "X", "album": None, "title": "Song", "duration": 12.0, "ext": ".mp3", "file_size": 5}
        ]).claim(fields, 5))                                      # different length


if __name__ == "__main__":
    unittest.main(verbosity=2)
