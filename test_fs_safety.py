#!/usr/bin/env python3
"""Unit tests for fs_safety.safe_move.

No real exFAT/FAT32/NTFS volume is available in this environment to test
against (hdiutil/diskutil image creation isn't permitted here) -- these
tests instead simulate the EXDEV cross-device path and the unlink-failure
escalation path directly, which covers safe_move's actual logic. They
canNOT reproduce the real "needs a fresh process" quirk itself, which was
only ever observed against a real, actively-Spotlight-indexed physical
exFAT drive under sustained load -- that's a mitigation for a reproduced-
but-not-fully-understood failure, not something a unit test can prove
fixes anything. What these tests do prove: the copy+verify step is
correct, the retry-then-subprocess-escalation sequence actually removes
the source when os.unlink keeps failing, and a total failure degrades to
the non-fatal OrphanedSourceWarning (still an OSError, so existing per-
file try/except call sites catch it) rather than corrupting anything.

Run manually:

    python3 test_fs_safety.py
"""
import errno
import os
import shutil
import tempfile
import unittest
from unittest import mock

import fs_safety


class SafeMoveTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="jukebox-fssafety-")
        self.src = os.path.join(self.tmpdir, "source.txt")
        self.dst = os.path.join(self.tmpdir, "dest.txt")
        with open(self.src, "wb") as f:
            f.write(b"hello world" * 100)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_same_device_rename_fast_path(self):
        fs_safety.safe_move(self.src, self.dst)
        self.assertFalse(os.path.exists(self.src))
        self.assertTrue(os.path.isfile(self.dst))

    def test_simulated_cross_device_copy_and_unlink(self):
        real_rename = os.rename

        def fake_rename(src, dst):
            if src == self.src:
                raise OSError(errno.EXDEV, "Cross-device link")
            return real_rename(src, dst)

        with mock.patch("os.rename", side_effect=fake_rename):
            fs_safety.safe_move(self.src, self.dst)
        self.assertFalse(os.path.exists(self.src))
        self.assertTrue(os.path.isfile(self.dst))

    def test_size_mismatch_cleans_up_and_raises(self):
        def fake_copy2(src, dst):
            with open(dst, "wb") as f:
                f.write(b"short")  # deliberately wrong size

        with mock.patch("os.rename", side_effect=OSError(errno.EXDEV, "x")), \
             mock.patch("shutil.copy2", side_effect=fake_copy2):
            with self.assertRaises(OSError):
                fs_safety.safe_move(self.src, self.dst)
        self.assertFalse(os.path.exists(self.dst), "the bad partial copy must not be left behind")
        self.assertTrue(os.path.isfile(self.src), "source must be untouched on a verification failure")

    def test_unlink_failure_escalates_to_subprocess_and_succeeds(self):
        """Forces the exact failure mode this module exists for: the copy
        succeeds, but every os.unlink attempt on the source raises
        FileNotFoundError even though the file is still really there.
        Confirms the subprocess `rm -f` fallback still gets the source
        removed."""
        with mock.patch("os.rename", side_effect=OSError(errno.EXDEV, "x")), \
             mock.patch("os.unlink", side_effect=FileNotFoundError(2, "No such file or directory")), \
             mock.patch("time.sleep"):  # don't actually wait through the retry backoff
            fs_safety.safe_move(self.src, self.dst)
        self.assertTrue(os.path.isfile(self.dst))
        self.assertFalse(os.path.isfile(self.src), "subprocess rm -f should have removed the real source")

    def test_total_failure_degrades_to_orphaned_source_warning(self):
        """If even the subprocess escalation can't remove the source (a
        permission issue, say), this must not pretend the move fully
        succeeded, but also must not corrupt the already-good copy at
        dst -- and OrphanedSourceWarning must be an OSError subclass so
        every existing per-file try/except OSError call site
        (_delete_track_rows, organize_by_artist.organize(), etc) already
        catches it and keeps going instead of crashing the whole batch."""
        with mock.patch("os.rename", side_effect=OSError(errno.EXDEV, "x")), \
             mock.patch("os.unlink", side_effect=FileNotFoundError(2, "No such file or directory")), \
             mock.patch("subprocess.run"), \
             mock.patch("time.sleep"):
            with self.assertRaises(fs_safety.OrphanedSourceWarning) as ctx:
                fs_safety.safe_move(self.src, self.dst)
        self.assertIsInstance(ctx.exception, OSError)
        self.assertTrue(os.path.isfile(self.dst), "the good copy must survive even when the source can't be cleaned up")
        self.assertTrue(os.path.isfile(self.src))


if __name__ == "__main__":
    unittest.main()
