#!/usr/bin/env python3
"""Tidying copy numbers out of file names. Scratch library only.

Run manually:

    python3 test_filename_cleanup.py
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

TMPDIR = tempfile.mkdtemp(prefix="jukebox-tidy-")
MUSIC = os.path.join(TMPDIR, "music")
os.makedirs(MUSIC)
os.environ["JUKEBOX_DB_PATH"] = os.path.join(TMPDIR, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(TMPDIR, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = MUSIC

import app as m  # noqa: E402
import filename_cleanup as fc  # noqa: E402
import journal  # noqa: E402


class CleanNameTest(unittest.TestCase):
    def clean(self, filename, title=None, real=True):
        return fc.clean_name(filename, title, title_is_real=real and title is not None)[0]

    def test_parenthesised_copy_number_goes(self):
        self.assertEqual(self.clean("Annie Lennox - No More I Love You's (2).m4a", "No More I Love You's"),
                         "Annie Lennox - No More I Love You's.m4a")
        self.assertEqual(self.clean("Song(1).mp3", "Song"), "Song.mp3")
        self.assertEqual(self.clean("Untagged Download (3).mp3"), "Untagged Download.mp3")  # no tag needed for "(n)"
        self.assertEqual(self.clean("Song (2) (1).mp3", "Song"), "Song.mp3")

    def test_bare_number_goes_only_when_provably_extra(self):
        self.assertEqual(self.clean("Queen - Bohemian Rhapsody 1.mp3", "Bohemian Rhapsody"), "Queen - Bohemian Rhapsody.mp3")
        self.assertEqual(self.clean("Queen - Bohemian Rhapsody_2.mp3", "Bohemian Rhapsody"), "Queen - Bohemian Rhapsody.mp3")
        self.assertEqual(self.clean("Song copy.mp3", "Song"), "Song.mp3")
        self.assertEqual(self.clean("Song copy 2.mp3", "Song"), "Song.mp3")

    def test_numbers_that_belong_to_the_title_stay(self):
        # every one of these is a real file in a real library
        for filename, title in [
            ("Brothers Johnson - Strawberry Letter 23.m4a", "Strawberry Letter 23"),
            ("George Michael - Freedom 90.m4a", "Freedom! '90"),
            ("Goldfrapp - Number 1.m4a", "Number 1"),
            ("The Beautiful South - Perfect 10.m4a", "Perfect 10"),
            ("Elastica - 21.m4a", "21"),
            ("Charles Dutoit - Danse Macabre, Op. 40.m4a", "Danse Macabre, Op. 40"),
            ("Elisabeth Leonskaja - Nocturne No. 2 in E-Flat Major, Op. 9, No. 2.m4a", "Nocturne No. 2 in E-Flat Major, Op. 9, No. 2"),
            ("Blink-182.mp3", "Blink-182"),
            ("Artist - Part (2).mp3", "Part (2)"),
            ("Artist - Carbon Copy.mp3", "Carbon Copy"),
        ]:
            self.assertIsNone(self.clean(filename, title), filename)

    def test_bare_number_without_proof_stays(self):
        self.assertIsNone(self.clean("Track 07.mp3", "Yesterday"))              # name doesn't end with the title
        self.assertIsNone(self.clean("Mambo No 5.mp3"))                         # no title tag at all
        self.assertIsNone(self.clean("Queen - Somebody 12.mp3", "Somebody"))    # only single digits are copy numbers
        self.assertIsNone(self.clean("Untagged copy.mp3"))
        self.assertIsNone(self.clean("Album (1999).mp3", "Album"))              # a year, not a copy number
        self.assertIsNone(self.clean("Plain Name.mp3", "Plain Name"))

    def test_doubled_extension(self):
        self.assertEqual(self.clean("Beyonce - Sweet Dreams.mp3.mp3", "Sweet Dreams"), "Beyonce - Sweet Dreams.mp3")
        self.assertEqual(self.clean("x.MP3.mp3"), "x.mp3")
        self.assertIsNone(self.clean("x.m4a.mp3"))  # not the same extension twice: leave it


def touch(rel, data=b"x"):
    path = os.path.join(MUSIC, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.get("/api/facets")  # builds the schema
        cls.rows = [
            # (path, artist, title, has_title_tag)
            ("Annie/Annie - Song (2).m4a", "Annie", "Song", 1),              # -> renamed
            ("Queen/Queen - Bo Rhap 1.mp3", "Queen", "Bo Rhap", 1),          # -> renamed
            ("Queen/Queen - Twins.mp3", "Queen", "Twins", 1),
            ("Queen/Queen - Twins (2).mp3", "Queen", "Twins", 1),            # blocked: plain twin exists
            ("Stevie/Stevie - Once (2).m4a", "Stevie", "Once", 1),           # lowest copy wins the plain name
            ("Stevie/Stevie - Once (3).m4a", "Stevie", "Once", 1),           # blocked: claimed by (2)
            ("George/George - Freedom 90.m4a", "George", "Freedom! '90", 1),  # untouched
            ("Gone/Gone - Missing (2).mp3", "Gone", "Missing", 1),           # not on disk: not offered
        ]
        conn = sqlite3.connect(m.DB_PATH)
        for path, artist, title, flag in cls.rows:
            if not path.startswith("Gone/"):
                touch(path, data=path.encode())
            conn.execute("INSERT INTO tracks (path, artist, title, ext, has_title_tag) VALUES (?,?,?,?,?)",
                         (path, artist, title, os.path.splitext(path)[1], flag))
        conn.commit()
        cls.ids = {r[1]: r[0] for r in conn.execute("SELECT id, path FROM tracks")}
        conn.execute("INSERT INTO ratings (track_id, rating) VALUES (?, 5)", (cls.ids["Annie/Annie - Song (2).m4a"],))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    def paths(self):
        conn = sqlite3.connect(m.DB_PATH)
        out = {r[0]: r[1] for r in conn.execute("SELECT id, path FROM tracks")}
        conn.close()
        return out

    def test_1_preview_offers_the_safe_ones_and_explains_the_rest(self):
        data = self.client.get("/api/tidy-names/preview").get_json()
        self.assertEqual({(i["old_name"], i["new_name"]) for i in data["items"]}, {
            ("Annie - Song (2).m4a", "Annie - Song.m4a"),
            ("Queen - Bo Rhap 1.mp3", "Queen - Bo Rhap.mp3"),
            ("Stevie - Once (2).m4a", "Stevie - Once.m4a"),
        })
        self.assertEqual({b["old_name"] for b in data["blocked"]}, {"Queen - Twins (2).mp3", "Stevie - Once (3).m4a"})
        self.assertTrue(all(b["why"] for b in data["blocked"]))
        self.assertTrue(os.path.isfile(os.path.join(MUSIC, "Annie/Annie - Song (2).m4a")))  # looking renamed nothing

    def test_2_apply_renames_only_what_was_asked_and_keeps_the_track(self):
        annie, queen = self.ids["Annie/Annie - Song (2).m4a"], self.ids["Queen/Queen - Bo Rhap 1.mp3"]
        blocked = self.ids["Queen/Queen - Twins (2).mp3"]
        r = self.client.post("/api/tidy-names", json={"ids": [annie, blocked]}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["renamed"], 1)
        self.assertEqual(r["errors"], [])
        paths = self.paths()
        self.assertEqual(paths[annie], "Annie/Annie - Song.m4a")                     # same row, so...
        self.assertTrue(os.path.isfile(os.path.join(MUSIC, "Annie/Annie - Song.m4a")))
        self.assertFalse(os.path.exists(os.path.join(MUSIC, "Annie/Annie - Song (2).m4a")))
        conn = sqlite3.connect(m.DB_PATH)
        self.assertEqual(conn.execute("SELECT rating FROM ratings WHERE track_id=?", (annie,)).fetchone()[0], 5)  # ...its rating stays
        conn.close()
        self.assertEqual(paths[queen], "Queen/Queen - Bo Rhap 1.mp3")                # not asked for
        self.assertEqual(paths[blocked], "Queen/Queen - Twins (2).mp3")              # never overwrites the twin
        with open(os.path.join(MUSIC, "Queen/Queen - Twins.mp3"), "rb") as f:
            self.assertEqual(f.read(), b"Queen/Queen - Twins.mp3")

    def test_3_the_rename_is_in_history_and_undo_puts_the_name_back(self):
        ops = journal.list_operations(m.DB_PATH)
        self.assertEqual(ops[0]["kind"], "tidy_names")
        self.assertEqual(ops[0]["label"], "Tidied 1 file name")
        result = journal.undo(m.DB_PATH, MUSIC, ops[0]["id"])
        self.assertEqual(result["restored"], 1)
        annie = self.ids["Annie/Annie - Song (2).m4a"]
        self.assertEqual(self.paths()[annie], "Annie/Annie - Song (2).m4a")
        self.assertTrue(os.path.isfile(os.path.join(MUSIC, "Annie/Annie - Song (2).m4a")))

    def test_4_bad_requests(self):
        self.assertEqual(self.client.post("/api/tidy-names", json={"ids": []}).status_code, 400)
        self.assertEqual(self.client.post("/api/tidy-names", json={"ids": ["1; DROP"]}).status_code, 400)
        r = self.client.post("/api/tidy-names", json={"ids": [999999]}).get_json()
        self.assertEqual(r["renamed"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
