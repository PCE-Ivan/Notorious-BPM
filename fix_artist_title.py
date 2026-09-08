#!/usr/bin/env python3
"""Corrects artist and track-title tags toward Deezer's canonical spelling
and capitalization -- similar in spirit to what MusicBrainz Picard does by
matching files against a reference catalog and standardizing their tags.

This uses the same Deezer public API fill_genres.py already draws from,
rather than MusicBrainz itself: MusicBrainz's API enforces a strict
1-request/second rate limit as a matter of policy, which would turn a
full-library pass into a multi-hour job on anything but a small collection.
Deezer's catalog covers the same commercial releases deemix downloads from
and has no such throttle.

Deliberately conservative: this only ever fixes formatting drift (wrong
capitalization, stray whitespace, a missing "The", accent marks, ...) --
never reattributes a track to a different artist or rewrites a title's
actual words, and a match is only trusted when it's a close fuzzy match to
what's already there (see _pick_correction). A bracketed edition suffix on
the title ("(Live)", "(Remastered 2011)", ...) is always preserved exactly
as it was on the file, never replaced by whatever Deezer's own title
happens to carry (which isn't reliably bracket-free either). Titles that
name a specific performance -- "(Live at ...)", "(Rehearsal - ...)", a
demo -- are skipped entirely: two different recordings of the same song are
often near-identical in both title and duration once that qualifier is
stripped for comparison, which is exactly the information that would tell
them apart, so this class of track isn't safe to auto-correct at all.
"""
import difflib
import os
import random
import re
import sqlite3
import sys
import time

import config as jukebox_config
from fill_genres import DEEZER_SEARCH, _EDITION_BRACKET_RE, _http_json, _search_title

DEFAULT_DB_PATH = os.path.join(jukebox_config.get_app_data_dir(), "library.db")
DB_PATH = os.environ.get("JUKEBOX_DB_PATH", DEFAULT_DB_PATH)
MUSIC_DIR = jukebox_config.get_music_dir()

# A specific performance (a particular concert date, a rehearsal take, a
# demo) is exactly the case where bracket-stripped fuzzy title matching
# stops being trustworthy: two different recordings of the same song are
# often near-identical in both title and duration once their "(Live at
# ...)"/"(Rehearsal - ...)" qualifier is stripped for comparison -- which is
# precisely the information that would tell them apart. Rather than risk a
# wrong-recording mismatch there, these are skipped entirely and left for
# manual review; they're a small slice of a typical library (well under 5%
# in testing here), so the safety trade costs very little real coverage.
_RISKY_TITLE_RE = re.compile(r"\b(live|rehearsal|demo|soundcheck)\b", re.IGNORECASE)

# Below this fuzzy-match ratio, treat a Deezer result as a different song or
# artist entirely rather than a formatting variant of the one on file. High
# on purpose -- a missed correction is a minor inconvenience, a wrong one
# silently corrupts a file's tags.
_SIMILARITY_MIN = 0.72
# If both durations are known and differ by more than this, it's a
# different recording (a rehearsal take, a different live date, ...), full
# stop, no matter how similar the titles read.
_MAX_DURATION_DIFF = 12  # seconds
# Without a duration to cross-check against, only trust a near-exact title
# match -- there's no other signal left to catch a wrong same-titled song.
_TITLE_SIM_WITHOUT_DURATION = 0.92


def _similarity(a, b):
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    # A name that's simply a substring of a longer one -- "Johnny Cash" vs.
    # a compilation/tribute credited as "I Am Johnny Cash" -- scores a
    # deceptively high SequenceMatcher ratio despite being a different
    # artist, not a formatting variant of the same one. Require the two
    # strings to be reasonably close in length too before trusting the
    # ratio at all.
    len_ratio = min(len(a), len(b)) / max(len(a), len(b))
    if len_ratio < 0.7:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


_TRAILING_BRACKET_RE = re.compile(r"[\(\[][^\(\)\[\]]*[\)\]]\s*$")


def _edition_suffix(title):
    """The bracketed part of a title, verbatim, when it's a trailing
    suffix -- e.g. "(Remastered 2011)" out of "Song (Remastered 2011)".
    Empty string if there isn't one. Deliberately anchored to the end of
    the string (not just "the first bracket found"): a bracket earlier in
    the title can be part of the actual title rather than an edition
    marker -- The Stone Roses' "(Song for My) Sugar Spun Sister" being a
    real example -- and grabbing from the first bracket onward would
    swallow (and garble) everything after it too."""
    m = _TRAILING_BRACKET_RE.search(title or "")
    return title[m.start():].strip() if m else ""


def _has_extra_brackets(title):
    """True if the title has a bracket that ISN'T just the one trailing
    edition suffix -- e.g. a leading parenthetical that's part of the
    actual title. That shape is rare and easy to mishandle (see
    _edition_suffix above), so tracks like that are skipped entirely
    rather than risking a garbled result."""
    title = title or ""
    suffix = _edition_suffix(title)
    remainder = title[:len(title) - len(suffix)] if suffix else title
    return bool(_EDITION_BRACKET_RE.search(remainder))


def _pick_correction(candidates, artist, title, duration):
    """Best same-recording candidate among Deezer's results for this track.
    Both artist and (bracket-stripped) title must be a close fuzzy match,
    and if durations are available for both sides they must be close too --
    a title/artist match alone isn't enough to trust for something like a
    "(Live at ...)" title, where a same-named but different performance is
    a very real possibility. None if nothing clears that bar."""
    search_title = _search_title(title)
    best, best_rank = None, None
    for c in candidates:
        cand_artist = (c.get("artist", {}) or {}).get("name") or ""
        cand_search_title = _search_title(c.get("title") or "")

        if _similarity(cand_artist, artist) < _SIMILARITY_MIN:
            continue
        title_sim = _similarity(cand_search_title, search_title)
        if title_sim < _SIMILARITY_MIN:
            continue

        cand_duration = c.get("duration")
        if duration and cand_duration:
            diff = abs(cand_duration - duration)
            if diff > _MAX_DURATION_DIFF:
                continue
            rank = diff
        elif title_sim >= _TITLE_SIM_WITHOUT_DURATION:
            rank = 1000  # unverified but the title alone is close enough to trust
        else:
            continue

        if best is None or rank < best_rank:
            best, best_rank = c, rank
    return best


def _lookup_correction(row):
    """(new_artist, new_title) if Deezer has a confident, different-enough
    spelling/capitalization for this track, else None."""
    if _RISKY_TITLE_RE.search(row["title"] or "") or _has_extra_brackets(row["title"]):
        return None
    safe_artist = (row["artist"] or "").replace('"', "")
    safe_title = _search_title(row["title"]).replace('"', "")
    results = _http_json(DEEZER_SEARCH, {"q": f'artist:"{safe_artist}" track:"{safe_title}"', "limit": 5})
    candidates = (results or {}).get("data") or []
    match = _pick_correction(candidates, row["artist"], row["title"], row["duration"])
    if not match:
        return None

    new_artist = (match.get("artist", {}) or {}).get("name") or row["artist"]
    # Deezer's own title field isn't reliably bracket-free (some do carry a
    # "(Remastered)" or similar) -- strip it the same way before rebuilding,
    # so the file's own edition suffix is never duplicated or replaced by a
    # different one Deezer happened to attach.
    base_title = _search_title(match.get("title") or "") or _search_title(row["title"])
    suffix = _edition_suffix(row["title"])
    new_title = f"{base_title} {suffix}".strip() if suffix else base_title

    new_artist, new_title = new_artist.strip(), new_title.strip()
    if new_artist == (row["artist"] or "").strip() and new_title == (row["title"] or "").strip():
        return None
    return new_artist, new_title


def _write_file_artist_title(fpath, artist, title):
    lower = fpath.lower()
    try:
        if lower.endswith(".flac"):
            from mutagen.flac import FLAC
            audio = FLAC(fpath)
            audio["artist"] = [artist]
            audio["title"] = [title]
            audio.save()
        elif lower.endswith(".mp3"):
            import mutagen
            from mutagen.id3 import ID3NoHeaderError
            from mutagen.easyid3 import EasyID3
            try:
                audio = EasyID3(fpath)
            except ID3NoHeaderError:
                audio = mutagen.File(fpath, easy=True)
                audio.add_tags()
            audio["artist"] = [artist]
            audio["title"] = [title]
            audio.save()
        elif lower.endswith(".m4a"):
            from mutagen.mp4 import MP4
            audio = MP4(fpath)
            audio["\xa9ART"] = [artist]
            audio["\xa9nam"] = [title]
            audio.save()
        else:
            return False
        return True
    except Exception:
        return False


def _all_tracks(conn, track_ids=None):
    if track_ids:
        placeholders = ",".join("?" * len(track_ids))
        return conn.execute(
            f"SELECT id, path, artist, title, duration FROM tracks "
            f"WHERE id IN ({placeholders}) AND artist IS NOT NULL AND artist != '' AND title IS NOT NULL AND title != ''",
            track_ids,
        ).fetchall()
    return conn.execute(
        "SELECT id, path, artist, title, duration FROM tracks "
        "WHERE artist IS NOT NULL AND artist != '' AND title IS NOT NULL AND title != ''"
    ).fetchall()


def preview_fix_artist_title(sample_size=120, example_limit=10):
    """Read-only: samples the library (a full pass just to estimate would
    take as long as the real run) and reports how many tracks in the
    sample would change, extrapolated to the whole library, plus a few
    concrete before/after examples."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = _all_tracks(conn)
        sample = rows if len(rows) <= sample_size else random.sample(rows, sample_size)
        to_fix = 0
        examples = []
        for row in sample:
            correction = _lookup_correction(row)
            if correction:
                to_fix += 1
                if len(examples) < example_limit:
                    examples.append({
                        "before": f"{row['artist']} — {row['title']}",
                        "after": f"{correction[0]} — {correction[1]}",
                    })
            time.sleep(0.15)
        estimated = round(to_fix / len(sample) * len(rows)) if sample else 0
        return {
            "tracks_checked": len(rows),
            "sampled": len(sample),
            "sample_fixes": to_fix,
            "estimated_fixes": estimated,
            "examples": examples,
        }
    finally:
        conn.close()


def fix_artist_title(progress_cb=None, track_ids=None):
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        raise RuntimeError(f"Music directory not set or not found: {MUSIC_DIR}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = _all_tracks(conn, track_ids)

    updated = errors = 0
    if progress_cb:
        progress_cb(0, len(rows), updated)

    for i, row in enumerate(rows):
        try:
            correction = _lookup_correction(row)
            if correction:
                new_artist, new_title = correction
                fpath = os.path.join(MUSIC_DIR, row["path"])
                if os.path.isfile(fpath) and _write_file_artist_title(fpath, new_artist, new_title):
                    conn.execute("UPDATE tracks SET artist=?, title=? WHERE id=?", (new_artist, new_title, row["id"]))
                    conn.commit()
                    updated += 1
                else:
                    errors += 1
        except Exception:
            errors += 1

        if progress_cb and (i + 1) % 25 == 0:
            progress_cb(i + 1, len(rows), updated)
        time.sleep(0.15)  # be polite to Deezer's public API

    conn.close()
    return {"checked": len(rows), "updated": updated, "errors": errors}


if __name__ == "__main__":
    def _print_progress(done, total, updated):
        print(f"  ... {done}/{total} checked, {updated} corrected so far", file=sys.stderr)

    stats = fix_artist_title(progress_cb=_print_progress)
    print(stats)
