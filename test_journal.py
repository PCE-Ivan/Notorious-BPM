#!/usr/bin/env python3
"""Tests for tagio.py (shared tag reader/writer) and journal.py (undo).
Real tiny audio files are generated with ffmpeg (skipped if it's missing).

Run manually:

    python3 test_journal.py
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

import journal
import scan_library
import tagio

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)


def make_audio(path, genre="Old", year="1999"):
    subprocess.run(
        [FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.3",
         "-metadata", "artist=Some Artist", "-metadata", "title=Some Title", "-metadata", f"genre={genre}",
         "-metadata", f"date={year}", path], check=True)


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jukebox-journal-")
        self.music = os.path.join(self.tmp, "music")
        os.makedirs(self.music)
        self.db = os.path.join(self.tmp, "lib.db")
        conn = sqlite3.connect(self.db)
        scan_library.build_schema(conn)
        conn.commit()
        conn.close()
        self.ids = {}
        for name in ("a.mp3", "b.flac", "c.m4a"):
            make_audio(os.path.join(self.music, name))
            conn = sqlite3.connect(self.db)
            cur = conn.execute(
                "INSERT INTO tracks (path, artist, title, genre, primary_genre, year, decade) VALUES (?,?,?,?,?,?,?)",
                (name, "Some Artist", "Some Title", "Old", "Old", 1999, 1990))
            conn.commit()
            self.ids[name] = cur.lastrowid
            conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.music, name)

    def track(self, name):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tracks WHERE id=?", (self.ids[name],)).fetchone()
        conn.close()
        return row

    def op(self, label="test op", batch=None):
        return journal.operation(self.db, self.music, "test", label, batch=batch)

    # ---- tagio
    def test_read_write_roundtrip_all_formats(self):
        for name in ("a.mp3", "b.flac", "c.m4a"):
            self.assertEqual(tagio.read_tag(self.path(name), "genre"), "Old", name)
            self.assertTrue(tagio.write_tags(self.path(name), {"genre": "Pop", "title": "New Title"}), name)
            self.assertEqual(tagio.read_tag(self.path(name), "genre"), "Pop", name)
            self.assertEqual(tagio.read_tag(self.path(name), "title"), "New Title", name)

    def test_empty_value_removes_the_tag(self):
        for name in ("a.mp3", "b.flac", "c.m4a"):
            self.assertTrue(tagio.write_tag(self.path(name), "genre", None))
            self.assertIsNone(tagio.read_tag(self.path(name), "genre"), name)

    def test_year_on_mp3_and_flac_also_sets_originaldate(self):
        for name in ("a.mp3", "b.flac"):
            tagio.write_tag(self.path(name), "year", 1985)
            self.assertEqual(tagio.read_tag(self.path(name), "year"), "1985")
            self.assertEqual(tagio.read_tag(self.path(name), "originaldate"), "1985")

    def test_unsupported_or_missing_file_returns_false(self):
        self.assertFalse(tagio.write_tag(os.path.join(self.music, "nope.mp3"), "genre", "x"))
        open(os.path.join(self.music, "x.txt"), "w").write("hi")
        self.assertFalse(tagio.write_tag(os.path.join(self.music, "x.txt"), "genre", "x"))

    # ---- journaling
    def test_writes_outside_an_operation_are_not_journaled(self):
        tagio.write_tag(self.path("a.mp3"), "genre", "Pop")
        self.assertEqual(journal.list_operations(self.db), [])

    def test_operation_records_changed_fields_only(self):
        with self.op("Changed genres"):
            tagio.write_tag(self.path("a.mp3"), "genre", "Pop")
            tagio.write_tag(self.path("b.flac"), "genre", "Old")  # unchanged -> not recorded
        ops = journal.list_operations(self.db)
        self.assertEqual(len(ops), 1)
        self.assertEqual((ops[0]["label"], ops[0]["item_count"], ops[0]["undone"]), ("Changed genres", 1, 0))

    def test_empty_operation_leaves_no_trace(self):
        with self.op():
            pass
        self.assertEqual(journal.list_operations(self.db), [])

    def test_undo_restores_files_and_index(self):
        with self.op():
            tagio.write_tags(self.path("a.mp3"), {"genre": "Pop", "year": 1985})
            tagio.write_tags(self.path("c.m4a"), {"genre": "Pop", "year": 1985})
            conn = sqlite3.connect(self.db)
            conn.execute("UPDATE tracks SET genre='Pop', primary_genre='Pop', year=1985, decade=1980")
            conn.commit(); conn.close()
        op_id = journal.list_operations(self.db)[0]["id"]
        result = journal.undo(self.db, self.music, op_id)
        self.assertEqual(result["errors"], 0)
        self.assertGreaterEqual(result["restored"], 4)
        for name in ("a.mp3", "c.m4a"):
            self.assertEqual(tagio.read_tag(self.path(name), "genre"), "Old")
            self.assertEqual(tagio.read_tag(self.path(name), "year"), "1999")
        row = self.track("a.mp3")
        self.assertEqual((row["genre"], row["primary_genre"], row["year"], row["decade"]), ("Old", "Old", 1999, 1990))
        self.assertEqual(tagio.read_tag(self.path("a.mp3"), "originaldate"), "1999")
        self.assertEqual(journal.list_operations(self.db)[0]["undone"], 1)

    def test_undo_removes_a_tag_that_didnt_exist_before(self):
        tagio.write_tag(self.path("a.mp3"), "genre", None, record=False)
        with self.op():
            tagio.write_tag(self.path("a.mp3"), "genre", "Pop")
        journal.undo(self.db, self.music, journal.list_operations(self.db)[0]["id"])
        self.assertIsNone(tagio.read_tag(self.path("a.mp3"), "genre"))

    def test_undo_skips_a_tag_edited_since(self):
        with self.op():
            tagio.write_tag(self.path("a.mp3"), "genre", "Pop")
        tagio.write_tag(self.path("a.mp3"), "genre", "Jazz")  # the user's later edit
        result = journal.undo(self.db, self.music, journal.list_operations(self.db)[0]["id"])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(tagio.read_tag(self.path("a.mp3"), "genre"), "Jazz")

    def test_batch_id_joins_separate_blocks_into_one_operation(self):
        for name in ("a.mp3", "b.flac", "c.m4a"):
            with self.op("Edited tags", batch="click-1"):
                tagio.write_tag(self.path(name), "genre", "Pop")
        ops = journal.list_operations(self.db)
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["item_count"], 3)

    def test_undo_of_moves(self):
        os.makedirs(os.path.join(self.music, "Artist"))
        shutil.move(self.path("a.mp3"), os.path.join(self.music, "Artist", "a.mp3"))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tracks SET path=? WHERE id=?", ("Artist/a.mp3", self.ids["a.mp3"]))
        conn.commit(); conn.close()
        with self.op("Organized"):
            journal.record_moves({"a.mp3": "Artist/a.mp3"})
        result = journal.undo(self.db, self.music, journal.list_operations(self.db)[0]["id"])
        self.assertEqual(result["restored"], 1)
        self.assertTrue(os.path.isfile(self.path("a.mp3")))
        self.assertEqual(self.track("a.mp3")["path"], "a.mp3")

    def test_old_operations_are_pruned(self):
        journal_keep = journal.KEEP_OPERATIONS
        journal.KEEP_OPERATIONS = 3
        try:
            for n in range(6):
                with self.op(f"op {n}"):
                    tagio.write_tag(self.path("a.mp3"), "genre", f"G{n}")
        finally:
            journal.KEEP_OPERATIONS = journal_keep
        labels = [o["label"] for o in journal.list_operations(self.db)]
        self.assertEqual(labels, ["op 5", "op 4", "op 3"])

    def test_cancelled_operation_keeps_what_it_did(self):
        class Stop(BaseException):
            pass
        with self.assertRaises(Stop):
            with self.op("cancelled"):
                tagio.write_tag(self.path("a.mp3"), "genre", "Pop")
                raise Stop()
        ops = journal.list_operations(self.db)
        self.assertEqual((ops[0]["item_count"], ops[0]["finished_at"] is not None), (1, True))


if __name__ == "__main__":
    unittest.main()
