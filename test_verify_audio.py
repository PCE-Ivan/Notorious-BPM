#!/usr/bin/env python3
"""Unit tests for app.py's audio-fingerprint verification feature
(_tags_look_mismatched, _fingerprint_lookup) -- no real fpcalc binary or
network calls; fpcalc/AcoustID are mocked so this runs anywhere.

Run manually:

    python3 test_verify_audio.py
"""
import json
import os
import tempfile
import unittest
from unittest import mock


class TagsLookMismatchedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-verifytest-")
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.tmpdir
        global app
        import app as app_module
        app = app_module

    def test_exact_match_is_not_a_mismatch(self):
        self.assertFalse(app._tags_look_mismatched("Falco", "Rock Me Amadeus", "Falco", "Rock Me Amadeus"))

    def test_real_mismatch_is_caught(self):
        # The actual case this feature exists for: a file tagged with a
        # different, genuinely real song's info.
        self.assertTrue(app._tags_look_mismatched("A Flock of Seagulls", "I Ran", "Falco", "Rock Me Amadeus"))

    def test_multi_artist_credit_formatting_difference_is_lenient(self):
        self.assertFalse(app._tags_look_mismatched(
            "Bill Medley & Jennifer Warnes", "The Time of My Life",
            "Bill Medley, Jennifer Warnes", "The Time of My Life",
        ))

    def test_case_and_punctuation_only_is_lenient(self):
        self.assertFalse(app._tags_look_mismatched("  falco", "rock me AMADEUS!", "Falco", "Rock Me Amadeus"))

    def test_edition_qualifier_is_lenient(self):
        self.assertFalse(app._tags_look_mismatched("Falco", "Rock Me Amadeus (Live)", "Falco", "Rock Me Amadeus"))

    def test_missing_current_tags_never_flagged(self):
        # Nothing to compare against -- shouldn't claim a mismatch just
        # because the file had no tags at all.
        self.assertFalse(app._tags_look_mismatched(None, None, "Falco", "Rock Me Amadeus"))


class FingerprintLookupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global app
        import app as app_module
        app = app_module

    def test_parses_best_scoring_match(self):
        fake_fpcalc_result = mock.Mock(returncode=0, stdout=json.dumps({"fingerprint": "AQAB...", "duration": 200}))
        fake_lookup_response = json.dumps({
            "status": "ok",
            "results": [
                {"score": 0.7, "recordings": [{"title": "Wrong Song", "artists": [{"name": "Someone Else"}]}]},
                {"score": 0.95, "recordings": [{"title": "Rock Me Amadeus", "artists": [{"name": "Falco"}]}]},
            ],
        }).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return fake_lookup_response

        with mock.patch("subprocess.run", return_value=fake_fpcalc_result), \
             mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            artist, title, score = app._fingerprint_lookup("/fake/path.m4a", "fake-key", "/fake/fpcalc")
        self.assertEqual((artist, title), ("Falco", "Rock Me Amadeus"))
        self.assertAlmostEqual(score, 0.95)

    def test_fpcalc_failure_returns_none(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1, stdout="")):
            artist, title, score = app._fingerprint_lookup("/fake/path.m4a", "fake-key", "/fake/fpcalc")
        self.assertEqual((artist, title, score), (None, None, None))

    def test_acoustid_error_status_raises(self):
        fake_fpcalc_result = mock.Mock(returncode=0, stdout=json.dumps({"fingerprint": "AQAB...", "duration": 200}))
        fake_lookup_response = json.dumps({"status": "error", "error": {"message": "invalid API key"}}).encode("utf-8")

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return fake_lookup_response

        with mock.patch("subprocess.run", return_value=fake_fpcalc_result), \
             mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaises(RuntimeError):
                app._fingerprint_lookup("/fake/path.m4a", "bad-key", "/fake/fpcalc")


if __name__ == "__main__":
    unittest.main()
