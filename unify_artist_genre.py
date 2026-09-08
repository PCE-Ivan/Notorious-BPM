#!/usr/bin/env python3
"""Keeps one consistent genre per artist across their whole library, instead
of a different value per album/track -- which happens naturally, since a
track's genre usually just reflects however that particular release was
tagged. For each artist with more than one distinct genre among their
tracks, this picks the most common one (by track count, tied broken
alphabetically so re-running never flip-flops) and writes it into every one
of that artist's tracks -- both the file's own tag and the local index --
using the same file-writing logic as fill_genres.py.

Artists whose tracks already agree are left completely untouched (no
unnecessary file writes), and a track with no genre at all is left for the
dedicated fill_genres.py (Deezer lookup) unless its artist already needs
fixing for a real disagreement, in which case it's backfilled with the
dominant genre along with the rest.
"""
import os
import sqlite3
import sys
from collections import Counter

import config as jukebox_config
from fill_genres import _write_file_genre

DEFAULT_DB_PATH = os.path.join(jukebox_config.get_app_data_dir(), "library.db")
DB_PATH = os.environ.get("JUKEBOX_DB_PATH", DEFAULT_DB_PATH)
MUSIC_DIR = jukebox_config.get_music_dir()


def _normalize_artist(artist):
    return (artist or "").strip().lower()


def _plan(conn):
    """Read-only: groups tracks by artist, works out which artists disagree
    on genre and what each one's dominant genre would become. Shared by the
    preview (counts only, no writes) and the real run below."""
    rows = conn.execute(
        "SELECT id, path, artist, primary_genre FROM tracks WHERE artist IS NOT NULL AND artist != ''"
    ).fetchall()

    by_artist = {}
    for row in rows:
        key = _normalize_artist(row["artist"])
        if key:
            by_artist.setdefault(key, []).append(row)

    to_fix = {}
    for key, artist_rows in by_artist.items():
        present = {(r["primary_genre"] or "").strip() for r in artist_rows if (r["primary_genre"] or "").strip()}
        if len(present) <= 1:
            continue
        counts = Counter((r["primary_genre"] or "").strip() for r in artist_rows if (r["primary_genre"] or "").strip())
        top_count = max(counts.values())
        dominant = sorted(g for g, c in counts.items() if c == top_count)[0]
        changed_rows = [r for r in artist_rows if (r["primary_genre"] or "").strip() != dominant]
        to_fix[key] = (artist_rows[0]["artist"], dominant, changed_rows)

    return len(by_artist), to_fix


def preview_unify_artist_genres():
    """Fast, read-only counts for a confirmation prompt before the real
    (file-writing) run -- unlike fill_genres/fill_years, which only ever
    correct data toward an externally-verified value, this is a majority
    vote over locally-existing tags, a broader and more subjective change
    that's worth letting someone see the scope of first."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        artists_checked, to_fix = _plan(conn)
        tracks_to_update = sum(len(changed_rows) for _artist, _dominant, changed_rows in to_fix.values())
        examples = [
            {"artist": artist, "genre": dominant, "tracks": len(changed_rows)}
            for artist, dominant, changed_rows in sorted(to_fix.values(), key=lambda v: -len(v[2]))[:10]
        ]
        return {
            "artists_checked": artists_checked,
            "artists_to_fix": len(to_fix),
            "tracks_to_update": tracks_to_update,
            "examples": examples,
        }
    finally:
        conn.close()


def unify_artist_genres(progress_cb=None):
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        raise RuntimeError(f"Music directory not set or not found: {MUSIC_DIR}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    artists_checked, to_fix = _plan(conn)

    updated = errors = 0
    artists_total = len(to_fix)
    if progress_cb:
        progress_cb(0, artists_total, updated)

    for i, (_artist, dominant, changed_rows) in enumerate(to_fix.values()):
        for row in changed_rows:
            fpath = os.path.join(MUSIC_DIR, row["path"])
            try:
                if os.path.isfile(fpath) and _write_file_genre(fpath, dominant):
                    conn.execute(
                        "UPDATE tracks SET genre=?, primary_genre=? WHERE id=?",
                        (dominant, dominant, row["id"]),
                    )
                    updated += 1
                else:
                    errors += 1
            except Exception:
                errors += 1
        conn.commit()

        if progress_cb:
            progress_cb(i + 1, artists_total, updated)

    conn.close()
    return {"artists_checked": artists_checked, "artists_fixed": artists_total, "tracks_updated": updated, "errors": errors}


if __name__ == "__main__":
    def _print_progress(done, total, updated):
        print(f"  ... {done}/{total} artists checked, {updated} tracks updated so far", file=sys.stderr)

    stats = unify_artist_genres(progress_cb=_print_progress)
    print(stats)
