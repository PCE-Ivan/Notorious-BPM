#!/usr/bin/env python3
"""Reorganizes the music library on disk into one folder per Artist.

Moves every audio file (per scan_library.EXTS) directly into
MUSIC_DIR/<Artist>/<original filename>, using the same ID3 'artist' tag
scan_library.py reads for the library index -- so the resulting folder
layout matches what the app already shows as each track's Artist. Files
with no artist tag land under "Unknown Artist" rather than being skipped
outright. A file already sitting directly under its correct Artist folder
is left alone; a name collision at the destination (two different files
landing on the same target path, e.g. "01 Intro.mp3" from two different
albums by the same artist) gets a "(2)", "(3)", ... suffix rather than
overwriting anything.

Run this any time you want the on-disk layout to match Artist tags.
"""
import os
import re
import shutil
import sys

import mutagen

import config as jukebox_config
from scan_library import EXTS, _is_real_audio_file, first_or_none
from fs_safety import safe_move

MUSIC_DIR = jukebox_config.get_music_dir()

# Characters invalid in a folder name on Windows (the strictest of the
# platforms this app ships for) -- macOS/Linux tolerate more, but a name
# that works everywhere is safer than one that works only on the OS this
# happens to run on right now.
_INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_artist_name(name):
    # Windows also disallows a trailing dot or space on a folder name --
    # stripped here so the folder this creates doesn't quietly fail or get
    # silently renamed by the OS.
    name = _INVALID_CHARS_RE.sub("", name).strip().rstrip(". ")
    return name or "Unknown Artist"


def _unique_dest_path(dest_path):
    if not os.path.exists(dest_path):
        return dest_path
    base, ext = os.path.splitext(dest_path)
    n = 2
    while True:
        candidate = f"{base} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


def organize(progress_cb=None):
    """progress_cb(done, total, moved) -- same polling shape as
    scan_library.scan()'s progress_cb, with an extra `moved` count since
    `done` here also counts files skipped because they're already
    correctly placed."""
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        raise RuntimeError(f"Music directory not set or not found: {MUSIC_DIR}")

    files_to_check = []
    for root, dirs, files in os.walk(MUSIC_DIR):
        # Same exclusion as scan_library.py's own walk -- a staged-but-not-
        # yet-reviewed iPod import (MUSIC_DIR/.ipod_staging) has its own
        # artist-folder layout already; Organize should never reach into it.
        if root == MUSIC_DIR:
            dirs[:] = [d for d in dirs if d != ".ipod_staging"]
        for fname in files:
            # _is_real_audio_file excludes AppleDouble sidecar files
            # ("._Song.mp3") that macOS writes next to every real file on
            # any non-APFS/HFS+ volume (exFAT/FAT32/NTFS external drives)
            # to hold what it can't store natively -- these match EXTS just
            # like the real file but aren't audio at all. scan_library.py's
            # own walk already filters these the same way.
            if os.path.splitext(fname)[1].lower() in EXTS and _is_real_audio_file(fname):
                files_to_check.append(os.path.join(root, fname))

    total = len(files_to_check)
    done = 0
    moved = 0
    skipped = 0
    errors = []
    # old relpath -> new relpath, so a caller (app.py) can repoint existing
    # tracks.path rows at their new location instead of losing ratings/
    # playlists to a full library rebuild.
    path_moves = {}

    for fpath in files_to_check:
        done += 1
        try:
            audio = mutagen.File(fpath, easy=True)
            tags = dict(audio.tags) if audio and audio.tags else {}
            artist = first_or_none(tags, "artist")
        except Exception as e:
            print(f"  ! could not read {fpath}: {e}", file=sys.stderr)
            artist = None

        artist_dir = _sanitize_artist_name(artist or "Unknown Artist")
        dest_dir = os.path.join(MUSIC_DIR, artist_dir)
        dest_path = os.path.join(dest_dir, os.path.basename(fpath))

        if os.path.abspath(dest_path) == os.path.abspath(fpath):
            skipped += 1
        else:
            try:
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = _unique_dest_path(dest_path)
                old_rel = os.path.relpath(fpath, MUSIC_DIR)
                safe_move(fpath, dest_path)
                new_rel = os.path.relpath(dest_path, MUSIC_DIR)
                path_moves[old_rel] = new_rel
                moved += 1
            except OSError as e:
                errors.append(f"{os.path.relpath(fpath, MUSIC_DIR)}: {e}")

        if progress_cb and done % 10 == 0:
            progress_cb(done, total, moved)

    if progress_cb:
        progress_cb(done, total, moved)

    # Clean up now-empty subfolders left behind (e.g. an old Artist/Album
    # directory once its only track moved up to Artist/) -- bottom-up, and
    # re-checking with a fresh listdir() rather than trusting os.walk's own
    # (pre-computed) dirs/files lists, so a folder that's only empty once
    # its own now-removed children are gone actually gets caught too.
    for root, _dirs, _files in os.walk(MUSIC_DIR, topdown=False):
        if root == MUSIC_DIR:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass

    print(f"Done: {moved} moved, {skipped} already in place, {len(errors)} errors.")
    return {
        "total": total,
        "moved": moved,
        "skipped": skipped,
        "errors": errors[:20],  # cap -- thousands of errors would bloat the response for no benefit
        "error_count": len(errors),
        "path_moves": path_moves,
    }


if __name__ == "__main__":
    try:
        print(organize())
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
