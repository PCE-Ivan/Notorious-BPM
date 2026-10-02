#!/usr/bin/env python3
"""Albums / Artists grid routes and the album track filter. Scratch library only.

Run manually:

    python3 test_browse_routes.py
"""
import os
import shutil
import sqlite3
import tempfile
import unittest

TMPDIR = tempfile.mkdtemp(prefix="jukebox-browse-")
os.environ["JUKEBOX_DB_PATH"] = os.path.join(TMPDIR, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(TMPDIR, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = TMPDIR

import app as m  # noqa: E402

# (artist, album, title, genre, year, has_art)
ROWS = [
    ("Queen", "A Night at the Opera", "Death on Two Legs", "Rock", 1975, 1),
    ("Queen", "A Night at the Opera", "Bohemian Rhapsody", "Rock", 1975, 1),
    ("Queen", "News of the World", "We Will Rock You", "Rock", 1977, 0),
    ("ABBA", "Arrival", "Dancing Queen", "Pop", 1976, None),
    ("ABBA", "Arrival", "Fernando", "Pop", 1976, None),
    ("ABBA", "Arrival", "Money, Money, Money", "Pop", 1976, None),
    ("Solo Guy", "", "No Album Single", "Pop", 2001, 0),
    ("Various", "Greatest Hits", "Track A", "Pop", 1999, 0),
    ("Other", "Greatest Hits", "Track B", "Pop", 2005, 0),
]


class BrowseRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.get("/api/facets")
        conn = sqlite3.connect(m.DB_PATH)
        for i, (artist, album, title, genre, year, has_art) in enumerate(ROWS):
            conn.execute(
                "INSERT INTO tracks (path, artist, album, title, primary_genre, genre, year, decade, ext, duration, has_art) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"{i}.mp3", artist, album, title, genre, genre, year, (year // 10) * 10, ".mp3", 200.0, has_art))
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    def get(self, url):
        return self.client.get(url).get_json()

    def test_albums_group_by_album_and_artist(self):
        data = self.get("/api/albums")
        got = [(a["artist"], a["album"], a["n"]) for a in data["items"]]
        self.assertEqual(data["total"], 5)
        self.assertEqual(got, [
            ("ABBA", "Arrival", 3),
            ("Other", "Greatest Hits", 1),       # same title, different artist: separate cards
            ("Queen", "A Night at the Opera", 2),
            ("Queen", "News of the World", 1),
            ("Various", "Greatest Hits", 1),
        ])
        # tracks with no album never form an album
        self.assertNotIn("No Album Single", str(data))

    def test_album_cards_carry_art_hints(self):
        by = {(a["artist"], a["album"]): a for a in self.get("/api/albums")["items"]}
        opera = by[("Queen", "A Night at the Opera")]
        self.assertIsNotNone(opera["art_id"])               # known to have art
        self.assertIsNone(by[("Queen", "News of the World")]["art_id"])   # known not to
        self.assertIsNone(by[("Queen", "News of the World")]["maybe_id"])
        self.assertIsNotNone(by[("ABBA", "Arrival")]["maybe_id"])         # not checked yet
        self.assertEqual(by[("ABBA", "Arrival")]["year"], 1976)
        self.assertEqual(by[("ABBA", "Arrival")]["duration"], 600.0)

    def test_albums_respect_the_track_filters(self):
        data = self.get("/api/albums?genre=Rock")
        self.assertEqual({(a["artist"], a["album"]) for a in data["items"]},
                         {("Queen", "A Night at the Opera"), ("Queen", "News of the World")})
        data = self.get("/api/albums?q=fernando")
        self.assertEqual([(a["album"], a["n"]) for a in data["items"]], [("Arrival", 1)])

    def test_albums_sorting_and_paging(self):
        by_year = [a["album"] for a in self.get("/api/albums?sort=year")["items"]]
        self.assertEqual(by_year[0], "Greatest Hits")          # 2005, newest first
        by_count = self.get("/api/albums?sort=count")["items"]
        self.assertEqual(by_count[0]["album"], "Arrival")
        page = self.get("/api/albums?limit=2&offset=2")
        self.assertEqual((len(page["items"]), page["total"]), (2, 5))

    def test_artists(self):
        data = self.get("/api/artists")
        got = {a["artist"]: (a["n"], a["albums"]) for a in data["items"]}
        self.assertEqual(got["Queen"], (3, 2))
        self.assertEqual(got["ABBA"], (3, 1))
        self.assertIn("Solo Guy", got)
        self.assertEqual(self.get("/api/artists?sort=count")["items"][0]["n"], 3)
        self.assertEqual([a["artist"] for a in self.get("/api/artists?genre=Rock")["items"]], ["Queen"])

    def test_tracks_can_be_filtered_by_album(self):
        data = self.get("/api/tracks?album=A%20Night%20at%20the%20Opera&artist=Queen")
        self.assertEqual(data["total"], 2)
        self.assertEqual({t["title"] for t in data["tracks"]}, {"Death on Two Legs", "Bohemian Rhapsody"})
        # the same album title by another artist is a different album
        data = self.get("/api/tracks?album=Greatest%20Hits&artist=Other")
        self.assertEqual([t["title"] for t in data["tracks"]], ["Track B"])

    def test_bad_paging_is_a_400(self):
        self.assertEqual(self.client.get("/api/albums?limit=lots").status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
