#!/usr/bin/env python3
"""Unit tests for art_lookup.py's fallback chain -- mocked HTTP, no real
network calls (the manual verification against real tracks/APIs already
happened in-session; these lock in the fallback ORDER and short-circuit
behavior without depending on any external service being up).

Run manually:

    python3 test_art_lookup.py
"""
import unittest
from unittest import mock

import art_lookup


class FindCoverUrlTest(unittest.TestCase):
    def test_stops_at_first_source_that_finds_something(self):
        with mock.patch.object(art_lookup, "_deezer_cover_url", return_value="http://deezer/x.jpg") as deezer, \
             mock.patch.object(art_lookup, "_itunes_cover_url") as itunes, \
             mock.patch.object(art_lookup, "_musicbrainz_cover_url") as mb:
            url, source = art_lookup.find_cover_url("Artist", "Title")
        self.assertEqual((url, source), ("http://deezer/x.jpg", "deezer"))
        deezer.assert_called_once()
        itunes.assert_not_called()
        mb.assert_not_called()

    def test_falls_through_to_second_source(self):
        with mock.patch.object(art_lookup, "_deezer_cover_url", return_value=None), \
             mock.patch.object(art_lookup, "_itunes_cover_url", return_value="http://itunes/x.jpg") as itunes, \
             mock.patch.object(art_lookup, "_musicbrainz_cover_url") as mb:
            url, source = art_lookup.find_cover_url("Artist", "Title")
        self.assertEqual((url, source), ("http://itunes/x.jpg", "itunes"))
        itunes.assert_called_once()
        mb.assert_not_called()

    def test_falls_through_to_third_source(self):
        with mock.patch.object(art_lookup, "_deezer_cover_url", return_value=None), \
             mock.patch.object(art_lookup, "_itunes_cover_url", return_value=None), \
             mock.patch.object(art_lookup, "_musicbrainz_cover_url", return_value="http://coverartarchive/x.jpg"):
            url, source = art_lookup.find_cover_url("Artist", "Title")
        self.assertEqual((url, source), ("http://coverartarchive/x.jpg", "musicbrainz"))

    def test_no_source_matches(self):
        with mock.patch.object(art_lookup, "_deezer_cover_url", return_value=None), \
             mock.patch.object(art_lookup, "_itunes_cover_url", return_value=None), \
             mock.patch.object(art_lookup, "_musicbrainz_cover_url", return_value=None):
            url, source = art_lookup.find_cover_url("Artist", "Title")
        self.assertEqual((url, source), (None, None))

    def test_a_source_raising_is_treated_as_a_miss_not_a_crash(self):
        with mock.patch.object(art_lookup, "_deezer_cover_url", side_effect=RuntimeError("network blip")), \
             mock.patch.object(art_lookup, "_itunes_cover_url", return_value="http://itunes/x.jpg"), \
             mock.patch.object(art_lookup, "_musicbrainz_cover_url") as mb:
            url, source = art_lookup.find_cover_url("Artist", "Title")
        self.assertEqual((url, source), ("http://itunes/x.jpg", "itunes"))
        mb.assert_not_called()

    def test_itunes_upsizes_thumbnail_url(self):
        with mock.patch.object(art_lookup, "_http_json", return_value={
            "results": [{"artistName": "Artist", "artworkUrl100": "http://a/100x100bb.jpg"}]
        }):
            url = art_lookup._itunes_cover_url("Artist", "Title")
        self.assertEqual(url, "http://a/1200x1200bb.jpg")


if __name__ == "__main__":
    unittest.main()
