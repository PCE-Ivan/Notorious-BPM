#!/usr/bin/env python3
"""1.3 additions: column sorting, bulk rating, ratings saved in files, renaming
to a pattern, version comparison, richer stats. Scratch library only.

Run manually:

    python3 test_v13_features.py
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest

TMPDIR = tempfile.mkdtemp(prefix="jukebox-v13-")
MUSIC = os.path.join(TMPDIR, "music")
os.makedirs(MUSIC)
os.environ["JUKEBOX_DB_PATH"] = os.path.join(TMPDIR, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(TMPDIR, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = MUSIC

import app as m  # noqa: E402
import filename_cleanup as fc  # noqa: E402
import rating_tags  # noqa: E402
import version  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else None)

# (file, artist, album, title, seconds)
TRACKS = [
    ("Queen/01 track.mp3", "Queen", "Jazz", "Bicycle Race", 3),
    ("Queen/02 track.mp3", "Queen", "A Day", "Seaside", 1),
    ("ABBA/x.flac", "ABBA", "Arrival", "Tiger", 2),
    ("ABBA/y.m4a", "ABBA", "", "Why: Me?", 4),
]


def make(rel, artist, album, title, seconds):
    path = os.path.join(MUSIC, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = ["-metadata", f"artist={artist}", "-metadata", f"title={title}"]
    if album:
        meta += ["-metadata", f"album={album}"]
    subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
                    *meta, path], check=True)


class VersionTest(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(version.is_newer("v1.4", "1.3"))
        self.assertTrue(version.is_newer("1.3.1", "1.3"))
        self.assertTrue(version.is_newer("1.10", "1.9"))
        self.assertFalse(version.is_newer("v1.3", "1.3"))
        self.assertFalse(version.is_newer("1.2", "1.3"))
        self.assertFalse(version.is_newer("nightly", "1.3"))
        self.assertFalse(version.is_newer("", "1.3"))


class PatternNameTest(unittest.TestCase):
    def test_patterns(self):
        self.assertEqual(fc.pattern_name("01 track.MP3", "artist-title", "AC/DC", None, "Back: In Black?", True)[0],
                         "AC-DC - Back - In Black.mp3")
        self.assertEqual(fc.pattern_name("a.flac", "artist-album-title", "A", "B", "C", True)[0], "A - B - C.flac")
        self.assertEqual(fc.pattern_name("a.flac", "title", "A", "B", "C", True)[0], "C.flac")

    def test_missing_pieces_leave_the_file_alone(self):
        self.assertIsNone(fc.pattern_name("a.mp3", "artist-album-title", "A", "", "C", True)[0])   # no album
        self.assertIsNone(fc.pattern_name("a.mp3", "artist-title", "A", "B", "a", False)[0])       # title isn't a real tag
        self.assertIsNone(fc.pattern_name("A - C.mp3", "artist-title", "A", "B", "C", True)[0])    # already right
        self.assertIsNone(fc.pattern_name("a.mp3", "nonsense", "A", "B", "C", True)[0])


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()
        cls.client.get("/api/facets")
        for t in TRACKS:
            make(*t)
        cls.client.post("/api/rescan", json={})
        cls.wait("/api/scan-progress")
        cls.by_title = {t["title"]: t for t in cls.client.get("/api/tracks?limit=50").get_json()["tracks"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    @classmethod
    def wait(cls, url, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if not cls.client.get(url).get_json().get("running"):
                return
            time.sleep(0.05)
        raise AssertionError("job did not finish: " + url)

    def titles(self, query):
        return [t["title"] for t in self.client.get("/api/tracks?" + query).get_json()["tracks"]]

    def path_of(self, track_id):
        conn = sqlite3.connect(m.DB_PATH)
        path = conn.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()[0]
        conn.close()
        return path

    def test_1_sorting_by_any_column_both_ways(self):
        self.assertEqual(self.titles("sort=duration"), ["Seaside", "Tiger", "Bicycle Race", "Why: Me?"])
        self.assertEqual(self.titles("sort=duration&dir=desc")[0], "Why: Me?")
        self.assertEqual(self.titles("sort=title&dir=desc")[0], "Why: Me?")
        albums = self.titles("sort=album")
        self.assertEqual(albums[:3], ["Seaside", "Tiger", "Bicycle Race"])   # A Day, Arrival, Jazz
        self.assertEqual(albums[-1], "Why: Me?")                             # no album: last, either way
        self.assertEqual(self.titles("sort=album&dir=desc")[-1], "Why: Me?")
        self.assertEqual(len(self.titles("sort=bogus&dir=sideways")), 4)     # falls back, never a 500

    def test_2_bulk_rating(self):
        ids = [self.by_title["Tiger"]["id"], self.by_title["Seaside"]["id"]]
        r = self.client.post("/api/rate-tracks", json={"track_ids": ids + [999999], "rating": 4}).get_json()
        self.assertEqual(r["updated"], 2)
        rated = {t["title"]: t["rating"] for t in self.client.get("/api/tracks?limit=50").get_json()["tracks"]}
        self.assertEqual((rated["Tiger"], rated["Seaside"], rated["Bicycle Race"]), (4, 4, None))
        self.client.post("/api/rate-tracks", json={"track_ids": [ids[1]], "rating": 0})
        rated = {t["title"]: t["rating"] for t in self.client.get("/api/tracks?limit=50").get_json()["tracks"]}
        self.assertIsNone(rated["Seaside"])
        self.assertEqual(self.client.post("/api/rate-tracks", json={"track_ids": ids, "rating": 9}).status_code, 400)
        self.assertEqual(self.client.post("/api/rate-tracks", json={"track_ids": [], "rating": 3}).status_code, 400)

    def test_3_rating_tags_round_trip_in_every_format(self):
        for rel in ("Queen/01 track.mp3", "ABBA/x.flac", "ABBA/y.m4a"):
            path = os.path.join(MUSIC, rel)
            for stars in (5, 1, 3):
                self.assertTrue(rating_tags.write(path, stars), rel)
                self.assertEqual(rating_tags.read(path), stars, rel)
            rating_tags.write(path, 0)
            self.assertEqual(rating_tags.read(path), 0, rel)
        self.assertEqual(rating_tags._stars_from_number("4"), 4)       # players that write 1..5
        self.assertEqual(rating_tags._stars_from_number("80"), 4)
        self.assertEqual(rating_tags._stars_from_popm(128), 3)

    def test_4_ratings_in_files_setting(self):
        self.assertFalse(self.client.get("/api/settings/ratings-in-files").get_json()["enabled"])
        tiger = self.by_title["Tiger"]["id"]                       # rated 4 in the library by test_2
        why = self.by_title["Why: Me?"]["id"]                      # unrated here...
        rating_tags.write(os.path.join(MUSIC, "ABBA/y.m4a"), 2)   # ...but its file says 2 (another computer)
        r = self.client.post("/api/settings/ratings-in-files", json={"enabled": True}).get_json()
        self.assertTrue(r["started"])
        self.wait("/api/settings/ratings-in-files/progress")
        self.assertEqual(rating_tags.read(os.path.join(MUSIC, self.path_of(tiger))), 4)   # library -> file
        rated = {t["id"]: t["rating"] for t in self.client.get("/api/tracks?limit=50").get_json()["tracks"]}
        self.assertEqual(rated[why], 2)                                                    # file -> library
        self.client.post("/api/rescan", json={})                  # (settles the file this test wrote by hand)
        self.wait("/api/scan-progress")
        # from now on a new rating goes straight into the file
        bike = self.by_title["Bicycle Race"]["id"]
        self.client.post(f"/api/rate/{bike}", json={"rating": 5})
        self.assertEqual(rating_tags.read(os.path.join(MUSIC, self.path_of(bike))), 5)
        # and a rescan doesn't see that write as a changed file
        self.client.post("/api/rescan", json={})
        self.wait("/api/scan-progress")
        self.assertEqual(self.client.get("/api/scan-progress").get_json()["result"]["updated"], 0)
        # a rating changed in the file elsewhere comes in on the next scan
        rating_tags.write(os.path.join(MUSIC, self.path_of(bike)), 1)
        self.client.post("/api/rescan", json={})
        self.wait("/api/scan-progress")
        rated = {t["id"]: t["rating"] for t in self.client.get("/api/tracks?limit=50").get_json()["tracks"]}
        self.assertEqual(rated[bike], 1)
        self.client.post("/api/settings/ratings-in-files", json={"enabled": False})
        self.assertFalse(self.client.get("/api/settings/ratings-in-files").get_json()["enabled"])

    def test_5_rename_to_a_pattern_and_undo_label(self):
        data = self.client.get("/api/tidy-names/preview?mode=artist-title").get_json()
        names = {i["old_name"]: i["new_name"] for i in data["items"]}
        self.assertEqual(names["01 track.mp3"], "Queen - Bicycle Race.mp3")
        self.assertEqual(names["y.m4a"], "ABBA - Why - Me.m4a")
        self.assertEqual({p["key"] for p in data["patterns"]}, set(fc.PATTERNS))
        bike = self.by_title["Bicycle Race"]["id"]
        r = self.client.post("/api/tidy-names", json={"ids": [bike], "mode": "artist-title"}).get_json()
        self.assertEqual(r["renamed"], 1)
        self.assertEqual(self.path_of(bike), "Queen/Queen - Bicycle Race.mp3")
        self.assertTrue(os.path.isfile(os.path.join(MUSIC, "Queen/Queen - Bicycle Race.mp3")))
        ops = m.journal.list_operations(m.DB_PATH)
        self.assertEqual(ops[0]["label"], "Renamed 1 file to “Artist - Title”")
        self.assertEqual(self.client.get("/api/tidy-names/preview?mode=nope").status_code, 400)
        # album pattern skips the track with no album instead of inventing one
        album = self.client.get("/api/tidy-names/preview?mode=artist-album-title").get_json()
        self.assertNotIn("y.m4a", {i["old_name"] for i in album["items"]})

    def test_6_stats_over_time_and_by_genre(self):
        tiger = self.by_title["Tiger"]["id"]
        self.client.post(f"/api/plays/{tiger}")
        self.client.post(f"/api/plays/{tiger}")
        data = self.client.get("/api/stats").get_json()
        self.assertEqual(len(data["by_day"]), 30)
        self.assertEqual(data["by_day"][-1]["plays"], 2)        # today
        self.assertEqual(sum(d["plays"] for d in data["by_day"]), 2)
        self.assertGreaterEqual(data["listening_seconds"], 3)   # 2 plays of a ~2 s track
        self.assertIn("top_genres", data)

    def test_7_about_reports_the_version(self):
        self.assertEqual(self.client.get("/api/about").get_json()["version"], version.VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
