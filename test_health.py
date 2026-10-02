#!/usr/bin/env python3
"""Tests for health.py against a scratch library in a temp dir.

Run manually:

    python3 test_health.py
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

import health
import scan_library


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jukebox-health-")
        self.music = os.path.join(self.tmp, "music")
        self.art = os.path.join(self.tmp, "art_cache")
        self.trash = os.path.join(self.tmp, "trash")
        self.backups = os.path.join(self.tmp, "backups")
        for d in (self.music, self.art, self.trash, self.backups):
            os.makedirs(d)
        self.db = os.path.join(self.tmp, "lib.db")
        conn = sqlite3.connect(self.db)
        scan_library.build_schema(conn)
        conn.execute("ALTER TABLE tracks ADD COLUMN has_art INTEGER")
        conn.execute(
            "CREATE TABLE trash (id INTEGER PRIMARY KEY AUTOINCREMENT, original_path TEXT NOT NULL, "
            "trash_path TEXT NOT NULL, artist TEXT, title TEXT, album TEXT, trashed_at TEXT NOT NULL)")
        conn.commit()
        conn.close()
        # a recent snapshot so "no_snapshot" doesn't muddy the healthy case
        open(os.path.join(self.backups, "library-20260101-000000.db"), "wb").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def add_track(self, name, has_art=0, create_file=True):
        path = f"{name}.mp3"
        if create_file:
            open(os.path.join(self.music, path), "wb").write(b"x")
        conn = sqlite3.connect(self.db)
        cur = conn.execute("INSERT INTO tracks (path, artist, title, has_art) VALUES (?,?,?,?)",
                           (path, "Artist", name, has_art))
        conn.commit()
        tid = cur.lastrowid
        conn.close()
        return tid

    def check(self, **kw):
        return health.run_checks(self.db, self.music, self.art, self.trash, self.backups, **kw)

    def ids(self, report):
        return {i["id"] for i in report["issues"]}

    def repair(self, ids):
        return health.repair(ids, self.db, self.music, self.art, self.trash)

    # ---- tests
    def test_clean_library_is_healthy(self):
        self.add_track("a")
        report = self.check()
        self.assertTrue(report["healthy"], report["issues"])
        self.assertEqual(report["tracks"], 1)

    def test_missing_files_detected_and_repaired_with_cascade(self):
        keep = self.add_track("keep")
        gone = self.add_track("gone", create_file=False)
        for n in range(8):
            self.add_track(f"extra{n}")  # keep the missing share under the 20% guard
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO playlists (name) VALUES ('p')")
        conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (1, ?, 0)", (gone,))
        conn.commit(); conn.close()
        report = self.check()
        issue = next(i for i in report["issues"] if i["id"] == "missing_files")
        self.assertEqual(issue["count"], 1)
        self.assertEqual(issue["repair"], "remove_missing_tracks")
        self.assertEqual(self.repair(["remove_missing_tracks"])["remove_missing_tracks"], 1)
        conn = sqlite3.connect(self.db)
        self.assertIsNone(conn.execute("SELECT 1 FROM tracks WHERE id=?", (gone,)).fetchone())
        self.assertIsNotNone(conn.execute("SELECT 1 FROM tracks WHERE id=?", (keep,)).fetchone())
        # foreign keys were on, so the playlist entry went with it
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0], 0)
        conn.close()

    def test_mass_missing_looks_like_unplugged_drive_and_is_never_auto_removed(self):
        for n in range(5):
            self.add_track(f"gone{n}", create_file=False)
        self.add_track("here")
        report = self.check()
        issue = next(i for i in report["issues"] if i["id"] == "missing_files")
        self.assertIsNone(issue["repair"])
        self.assertEqual(self.repair(["remove_missing_tracks"])["remove_missing_tracks"], 0)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0], 6)
        conn.close()

    def test_unreachable_music_folder_skips_file_checks(self):
        self.add_track("a")
        shutil.rmtree(self.music)
        report = self.check()
        self.assertIn("music_unreachable", self.ids(report))
        self.assertNotIn("missing_files", self.ids(report))

    def test_art_orphans_and_stale_none(self):
        tid = self.add_track("a")
        open(os.path.join(self.art, "9999.jpg"), "wb").close()
        open(os.path.join(self.art, "9999.thumb.jpg"), "wb").close()
        open(os.path.join(self.art, f"{tid}.jpg"), "wb").close()
        open(os.path.join(self.art, f"{tid}.none"), "wb").close()
        report = self.check()
        self.assertIn("art_orphans", self.ids(report))
        self.assertIn("art_stale_none", self.ids(report))
        res = self.repair(["delete_orphan_art", "clear_stale_none"])
        self.assertEqual(res["delete_orphan_art"], 2)
        self.assertEqual(res["clear_stale_none"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.art, f"{tid}.none")))
        self.assertTrue(os.path.exists(os.path.join(self.art, f"{tid}.jpg")))

    def test_art_flag_lies_both_directions(self):
        liar = self.add_track("liar", has_art=1)      # says art, none anywhere
        honest = self.add_track("honest", has_art=1)  # says art, has embedded art
        unmarked = self.add_track("unmarked", has_art=0)
        open(os.path.join(self.art, f"{unmarked}.jpg"), "wb").close()
        embedded = lambda path: path.endswith("honest.mp3")
        report = self.check(has_embedded_art=embedded)
        self.assertIn("art_flag_missing_image", self.ids(report))
        self.assertIn("art_flag_unset", self.ids(report))
        res = health.repair(["reset_art_flags", "set_art_flags"], self.db, self.music, self.art, self.trash,
                            has_embedded_art=embedded)
        self.assertEqual(res["reset_art_flags"], 1)
        self.assertEqual(res["set_art_flags"], 1)
        conn = sqlite3.connect(self.db)
        flags = dict(conn.execute("SELECT id, has_art FROM tracks").fetchall())
        conn.close()
        self.assertEqual(flags[liar], 0)
        self.assertEqual(flags[honest], 1)
        self.assertEqual(flags[unmarked], 1)

    def test_trash_dangling_rows_and_untracked_files(self):
        self.add_track("a")
        tracked = os.path.join(self.trash, "1_real.mp3")
        open(tracked, "wb").close()
        open(os.path.join(self.trash, "mystery.mp3"), "wb").close()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO trash (original_path, trash_path, trashed_at) VALUES ('x', ?, 't')", (tracked,))
        conn.execute("INSERT INTO trash (original_path, trash_path, trashed_at) VALUES ('y', ?, 't')",
                     (os.path.join(self.trash, "ghost.mp3"),))
        conn.commit(); conn.close()
        report = self.check()
        dangling = next(i for i in report["issues"] if i["id"] == "trash_dangling")
        self.assertEqual(dangling["count"], 1)
        untracked = next(i for i in report["issues"] if i["id"] == "trash_untracked")
        self.assertEqual(untracked["examples"], ["mystery.mp3"])
        self.assertIsNone(untracked["repair"])  # never auto-deleted
        self.assertEqual(self.repair(["delete_dangling_trash"])["delete_dangling_trash"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.trash, "mystery.mp3")))

    def test_dangling_links_from_deletes_without_foreign_keys(self):
        tid = self.add_track("a")
        conn = sqlite3.connect(self.db)  # foreign keys OFF, like the app's raw job connections
        conn.execute("INSERT INTO playlists (name) VALUES ('p')")
        conn.execute("INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (1, ?, 0)", (tid,))
        conn.execute("INSERT INTO ratings (track_id, rating) VALUES (?, 5)", (tid,))
        conn.execute("DELETE FROM tracks WHERE id=?", (tid,))
        conn.commit(); conn.close()
        report = self.check()
        issue = next(i for i in report["issues"] if i["id"] == "dangling_links")
        self.assertEqual(issue["count"], 2)
        self.assertEqual(self.repair(["delete_dangling_links"])["delete_dangling_links"], 2)
        self.assertNotIn("dangling_links", self.ids(self.check()))

    def test_stray_companion_folders_flagged(self):
        stray = os.path.join(self.tmp, "stray_parent")
        correct = os.path.join(self.tmp, "correct.nbpmdata")
        os.makedirs(os.path.join(stray, "art_cache"))
        report = self.check(stray_dir=stray, correct_dir=correct)
        self.assertIn("stray_companion", self.ids(report))
        called = []
        health.repair(["migrate_stray_dirs"], self.db, self.music, self.art, self.trash,
                      migrate_stray=lambda: called.append(1))
        self.assertEqual(called, [1])

    def test_progress_callback_can_cancel(self):
        self.add_track("a")
        class Stop(BaseException):
            pass
        def progress(done, total):
            if done >= 2:
                raise Stop()
        with self.assertRaises(Stop):
            self.check(progress=progress)

    def test_repairs_are_idempotent(self):
        self.add_track("ok")
        open(os.path.join(self.art, "77.jpg"), "wb").close()
        self.repair(["delete_orphan_art"])
        self.assertEqual(self.repair(["delete_orphan_art"])["delete_orphan_art"], 0)


if __name__ == "__main__":
    unittest.main()
