"""Hardened file-move helper for moving real audio files across filesystems
that aren't macOS's own APFS/HFS+ -- internal or external drives formatted
exFAT, FAT32, or NTFS for Windows compatibility, which this app needs to
work with reliably (see organize_by_artist.py's own AppleDouble-filter
comment for the sibling problem on the same class of drive).

Confirmed against a real exFAT external drive during a large iPod import +
duplicate cleanup: shutil.move()'s cross-device fallback (copy the file,
then unlink the source) had the copy always succeed, but the immediately-
following os.unlink(src) intermittently raised FileNotFoundError even
though the source file provably still existed (a separate process's
os.path.isfile() returned True at that exact moment). A same-process retry
loop (8 attempts, ~14s of backoff) never once fixed it; a fresh process
succeeded on the very first try, every single time, across 5-6 different
affected files. The leading (unconfirmed) hypothesis is some kernel- or
exFAT-driver-level directory-entry/vnode state tied to the calling
process's own file handle from the copy step, possibly worsened by
Spotlight actively re-indexing the same drive under heavy sustained load --
but this module doesn't claim to know the real root cause, only that
escalating to an actual subprocess (which gets the "fresh process" the bug
seems to care about, without restarting the whole app) reliably resolved
every occurrence observed.
"""
import errno
import os
import shutil
import subprocess
import time


class OrphanedSourceWarning(OSError):
    """Raised only when the destination copy is confirmed durable but the
    source file could not be removed by any means tried. Subclasses OSError
    on purpose: every existing caller already wraps its own move in a
    per-file `except OSError` that records the failure and moves on to the
    next file rather than aborting the whole batch (_delete_track_rows,
    organize_by_artist.organize(), ipod_import.py's staging/move loops) --
    this rides that same handling for free instead of needing every call
    site updated. The data is safe either way; there's just a leftover
    source file sitting where it shouldn't be."""


def safe_move(src, dst):
    """Like shutil.move(src, dst), but survives the cross-device unlink
    quirk described in this module's docstring. Same-device moves go
    through a plain, atomic os.rename and are unaffected."""
    try:
        os.rename(src, dst)
        return
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise

    shutil.copy2(src, dst)
    src_size = os.path.getsize(src)
    if os.path.getsize(dst) != src_size:
        os.remove(dst)
        raise OSError(f"Copy verification failed (size mismatch) for {src!r} -> {dst!r}")

    # Two quick retries -- cheap insurance against ordinary transient
    # contention, but the real bug this exists for was already proven NOT
    # to respond to same-process retrying, so this deliberately doesn't
    # burn much time here before escalating.
    last_err = None
    for _ in range(2):
        try:
            os.unlink(src)
            return
        except OSError as e:
            last_err = e
            time.sleep(0.2)

    # The one thing that reliably worked in every real occurrence: a fresh
    # process. `rm -f` also conveniently treats an already-gone source as
    # success rather than an error, which is the outcome that actually
    # matters here.
    subprocess.run(["rm", "-f", src], check=False)
    if not os.path.isfile(src):
        return

    raise OrphanedSourceWarning(
        f"Copied {src!r} to {dst!r} successfully, but couldn't remove the "
        f"original after retrying and a fresh-process fallback ({last_err})."
    )
