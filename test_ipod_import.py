#!/usr/bin/env python3
"""Unit tests for ipod_import.py's crash-recovery behavior.

Covers the gap where a real Classic iPod dropping off USB mid-copy
(confirmed in practice as `OSError: [Errno 6] Device not configured`, with
the iPod itself still mounted and healthy afterward) leaves a staging
folder that looked identical to a fully-finished batch, so clicking
"Import from iPod" again jumped straight to review with only the tracks
that made it in before the interruption. Builds a fully synthetic
(fake-mount, fake-iTunesDB) iPod rather than requiring a real device, using
the exact chunk layout ipod_import.parse_itunesdb reads.

Run manually:

    python3 test_ipod_import.py
"""
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("JUKEBOX_MUSIC_DIR", tempfile.gettempdir())
os.environ.setdefault("JUKEBOX_CONFIG_PATH", os.path.join(tempfile.gettempdir(), "jukebox-test-config.json"))

import ipod_import  # noqa: E402  (env vars above must be set first -- see organize_by_artist.py)


def _mhod_string(mhod_type, text):
    encoded = text.encode("utf-16-le")
    header_len = 40
    buf = bytearray(header_len + len(encoded))
    buf[0:4] = b"mhod"
    struct.pack_into("<I", buf, 4, header_len)
    struct.pack_into("<I", buf, 8, len(buf))
    struct.pack_into("<I", buf, 12, mhod_type)
    struct.pack_into("<I", buf, 28, len(encoded))
    buf[header_len:header_len + len(encoded)] = encoded
    return bytes(buf)


def _mhit(fields):
    mhods = [_mhod_string(t, v) for t, v in fields.items()]
    header_len = 16
    buf = bytearray(header_len)
    buf[0:4] = b"mhit"
    struct.pack_into("<I", buf, 4, header_len)
    struct.pack_into("<I", buf, 8, header_len + sum(len(m) for m in mhods))
    struct.pack_into("<I", buf, 12, len(mhods))
    for m in mhods:
        buf += m
    return bytes(buf)


def _mhlt(mhits):
    buf = bytearray(12)
    buf[0:4] = b"mhlt"
    struct.pack_into("<I", buf, 4, 12)
    struct.pack_into("<I", buf, 8, len(mhits))
    for m in mhits:
        buf += m
    return bytes(buf)


def _mhsd(mhlt_bytes, chunk_type=1):
    header_len = 16
    buf = bytearray(header_len)
    buf[0:4] = b"mhsd"
    struct.pack_into("<I", buf, 4, header_len)
    struct.pack_into("<I", buf, 8, header_len + len(mhlt_bytes))
    struct.pack_into("<I", buf, 12, chunk_type)
    buf += mhlt_bytes
    return bytes(buf)


def _mhbd(mhsd_bytes):
    header_len = 24
    buf = bytearray(header_len)
    buf[0:4] = b"mhbd"
    struct.pack_into("<I", buf, 4, header_len)
    struct.pack_into("<I", buf, 20, 1)
    buf += mhsd_bytes
    return bytes(buf)


def _build_fake_ipod(mount, n_tracks):
    """Writes a mount/iPod_Control/{iTunes/iTunesDB,Music/F00/*} tree that
    parse_itunesdb reads exactly like a real Classic iPod's, one track per
    (artist, title), each with distinct dummy file content."""
    music_dir = os.path.join(mount, "iPod_Control", "Music", "F00")
    os.makedirs(music_dir, exist_ok=True)
    mhits = []
    for i in range(n_tracks):
        fname = f"TRK{i:04d}.mp3"
        real_path = os.path.join(music_dir, fname)
        with open(real_path, "wb") as f:
            f.write(f"fake audio bytes for track {i}".encode() * (i + 1))
        mhits.append(_mhit({
            ipod_import.MHOD_TITLE: f"Title {i}",
            ipod_import.MHOD_ARTIST: f"Artist {i}",
            ipod_import.MHOD_ALBUM: "Album",
            ipod_import.MHOD_PATH: f":iPod_Control:Music:F00:{fname}",
        }))
    itunesdb_path = os.path.join(mount, "iPod_Control", "iTunes", "iTunesDB")
    os.makedirs(os.path.dirname(itunesdb_path), exist_ok=True)
    with open(itunesdb_path, "wb") as f:
        f.write(_mhbd(_mhsd(_mhlt(mhits))))
    return itunesdb_path


class ImportTracksResumeTest(unittest.TestCase):
    N_TRACKS = 5

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ipod-import-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.mount = os.path.join(self.tmpdir, "mount")
        self.music_dir = os.path.join(self.tmpdir, "library")
        os.makedirs(self.music_dir, exist_ok=True)
        # Same layout staging_root_for() gives app.py's real routes, so
        # list_pending_batches(self.music_dir) below sees this batch
        # exactly the way /api/ipod/import and the review screen would.
        self.staging_root = ipod_import.staging_root_for(self.music_dir, "TestPod")
        _build_fake_ipod(self.mount, self.N_TRACKS)

    def test_synthetic_ipod_parses(self):
        # Sanity check on the test fixture itself before trusting the
        # interruption/resume assertions below.
        tracks = ipod_import.parse_itunesdb(
            os.path.join(self.mount, "iPod_Control", "iTunes", "iTunesDB"), self.mount,
        )
        self.assertEqual(len(tracks), self.N_TRACKS)
        self.assertEqual(tracks[0]["title"], "Title 0")

    def test_interrupted_copy_leaves_batch_incomplete_and_resume_completes_it(self):
        real_copy2 = shutil.copy2
        calls_before_failure = 2  # fewer than N_TRACKS, so the "device drops mid-copy" case is exercised
        call_count = {"n": 0}

        def flaky_copy2(src, dst, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] > calls_before_failure:
                raise OSError(6, "Device not configured")
            return real_copy2(src, dst, *a, **kw)

        with mock.patch("ipod_import.shutil.copy2", side_effect=flaky_copy2):
            with self.assertRaises(OSError):
                ipod_import.import_tracks(self.mount, self.staging_root, self.music_dir)

        # Exactly the "looks identical to a finished batch" bug this fixes:
        # some files did land on disk, but the batch must not be reported
        # as done.
        staged_after_failure = ipod_import._staged_file_paths(self.staging_root)
        self.assertEqual(len(staged_after_failure), calls_before_failure)
        self.assertFalse(ipod_import.is_batch_complete(self.staging_root))

        # list_pending_batches (what app.py's /api/ipod/import and the
        # review screen actually consult) must agree it's incomplete too.
        pending = ipod_import.list_pending_batches(self.music_dir)
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0]["complete"])
        self.assertEqual(pending[0]["count"], calls_before_failure)

        # Re-running against the same staging_root (this time uninterrupted)
        # must pick up exactly what's missing, not redo or drop anything.
        result = ipod_import.import_tracks(self.mount, self.staging_root, self.music_dir)
        self.assertEqual(result["total"], self.N_TRACKS)
        self.assertEqual(result["already_staged"], calls_before_failure)
        self.assertEqual(result["copied"], self.N_TRACKS - calls_before_failure)
        self.assertTrue(ipod_import.is_batch_complete(self.staging_root))

        staged_after_resume = ipod_import._staged_file_paths(self.staging_root)
        self.assertEqual(len(staged_after_resume), self.N_TRACKS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
