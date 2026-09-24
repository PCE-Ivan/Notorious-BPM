#!/usr/bin/env python3
"""Unit tests for app.py's get_art() -- specifically its ability to derive a
thumbnail from an externally-fetched full-size cover (Deezer/iTunes/
MusicBrainz, via _fetch_and_cache_art) rather than only from a file's own
embedded art. Found this via a real bug report: a track whose only art came
from an external lookup showed correctly in the "now playing" panel (which
requests the full-size image) but never in the library list (which requests
a thumb) -- get_art()'s thumb path only ever re-derived from the audio
file's embedded art, which external-only tracks don't have, so it silently
failed and cached a permanent ".none" marker for art that clearly existed.

Run manually:

    python3 test_get_art.py
"""
import io
import os
import tempfile
import unittest
from unittest import mock


def _make_jpeg_bytes(color=(200, 30, 30), size=(64, 64)):
    from PIL import Image
    img = Image.new("RGB", size, color)
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


class GetArtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="jukebox-getarttest-")
        os.environ["JUKEBOX_DB_PATH"] = os.path.join(cls.tmpdir, "library.db")
        os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(cls.tmpdir, "config.json")
        os.environ["JUKEBOX_MUSIC_DIR"] = cls.tmpdir
        global app
        import app as app_module
        app = app_module

    def setUp(self):
        self.art_dir = tempfile.mkdtemp(prefix="jukebox-artcache-")
        self._patch = mock.patch.object(app, "ART_CACHE_DIR", self.art_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_thumb_derived_from_externally_fetched_full_image(self):
        # Simulate what _fetch_and_cache_art leaves behind: a full-size
        # cover cached on disk, no thumb, has_art set separately in the DB
        # (irrelevant to get_art, which only looks at the cache dir).
        with open(os.path.join(self.art_dir, "42.jpg"), "wb") as f:
            f.write(_make_jpeg_bytes())

        # fpath points at a file that doesn't even exist -- if get_art fell
        # through to _extract_art (embedded-art extraction) this would blow
        # up or fail, proving the full-cache path was actually used instead.
        data, mime = app.get_art(42, "/nonexistent/path.mp3", thumb=True)
        self.assertIsNotNone(data)
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(os.path.isfile(os.path.join(self.art_dir, "42.thumb.jpg")))

    def test_stale_none_marker_is_cleared_once_full_image_exists(self):
        # The exact race this bug produced: an earlier thumb request found
        # no full copy yet, fell back to embedded extraction, found nothing,
        # and wrote a permanent .none -- before the external fetch that was
        # already in flight finished writing the full image.
        with open(os.path.join(self.art_dir, "43.none"), "wb") as f:
            f.write(b"")
        with open(os.path.join(self.art_dir, "43.jpg"), "wb") as f:
            f.write(_make_jpeg_bytes())

        data, mime = app.get_art(43, "/nonexistent/path.mp3", thumb=True)
        self.assertIsNotNone(data)
        self.assertFalse(os.path.isfile(os.path.join(self.art_dir, "43.none")))

    def test_full_size_request_unaffected_by_thumb_fallback(self):
        with open(os.path.join(self.art_dir, "44.jpg"), "wb") as f:
            f.write(_make_jpeg_bytes())
        data, mime = app.get_art(44, "/nonexistent/path.mp3", thumb=False)
        self.assertIsNotNone(data)
        self.assertEqual(mime, "image/jpeg")

    def test_no_art_anywhere_still_writes_none_and_returns_none(self):
        with mock.patch.object(app, "_extract_art", return_value=(None, None)):
            data, mime = app.get_art(45, "/nonexistent/path.mp3", thumb=True)
        self.assertIsNone(data)
        self.assertIsNone(mime)
        self.assertTrue(os.path.isfile(os.path.join(self.art_dir, "45.none")))

    def test_embedded_art_still_generates_and_caches_both_sizes(self):
        embedded = _make_jpeg_bytes(color=(30, 200, 30))
        with mock.patch.object(app, "_extract_art", return_value=(embedded, "image/jpeg")):
            data, mime = app.get_art(46, "/nonexistent/path.mp3", thumb=True)
        self.assertIsNotNone(data)
        self.assertTrue(os.path.isfile(os.path.join(self.art_dir, "46.jpg")))
        self.assertTrue(os.path.isfile(os.path.join(self.art_dir, "46.thumb.jpg")))


if __name__ == "__main__":
    unittest.main()
