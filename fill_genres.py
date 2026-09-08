#!/usr/bin/env python3
"""On-demand enrichment: looks up missing genres via the public Deezer API
(the same catalog deemix downloads from, so its genre vocabulary already
matches what's in your other tags) and writes the result into both the
audio file's own tag and the local index -- so it survives future rescans,
not just a one-off database patch.
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter

import mutagen

import config as jukebox_config

DEFAULT_DB_PATH = os.path.join(jukebox_config.get_app_data_dir(), "library.db")
DB_PATH = os.environ.get("JUKEBOX_DB_PATH", DEFAULT_DB_PATH)
MUSIC_DIR = jukebox_config.get_music_dir()

DEEZER_SEARCH = "https://api.deezer.com/search"
DEEZER_ALBUM = "https://api.deezer.com/album/{}"

_album_genre_cache = {}

# A bracketed edition qualifier -- "(Remastered)", "(Live)", "(2011 Remaster
# Version)" -- almost never appears in Deezer's own track title, so leaving
# it in the search query returns zero matches for exactly the tracks this
# lookup exists to help with. Deezer's search engine already tolerates a
# missing dash-suffix ("Song - Live") reasonably well, so only brackets need
# stripping here.
_EDITION_BRACKET_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")


def _search_title(title):
    stripped = _EDITION_BRACKET_RE.sub("", title or "").strip()
    return stripped or title or ""


def _http_json(url, params=None, retries=3):
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(full_url, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.5)
    return None


def _pick_genre(candidates, artist, duration):
    """Different pressings/compilations of the same song are sometimes
    tagged with different (occasionally wrong) genres on Deezer -- e.g. a
    workout compilation mislabeling a metal track as "Pop". Rather than
    trust a single best-scoring match, take a majority vote across every
    same-artist candidate's album genre, tie-broken by closest duration.
    """
    artist_lower = (artist or "").strip().lower()
    qualifying = [
        r for r in candidates
        if artist_lower and artist_lower in (r.get("artist", {}).get("name") or "").strip().lower()
    ] or candidates[:1]

    seen_albums = set()
    album_candidates = []  # (album_id, result)
    for r in qualifying:
        aid = r.get("album", {}).get("id")
        if aid and aid not in seen_albums:
            seen_albums.add(aid)
            album_candidates.append((aid, r))

    genre_votes = Counter()
    genre_by_album = {}
    for aid, r in album_candidates:
        g = _genre_for_album(aid)
        if g:
            genre_votes[g] += 1
            genre_by_album[aid] = g

    if not genre_votes:
        return None
    top_count = max(genre_votes.values())
    tied = [g for g, c in genre_votes.items() if c == top_count]
    if len(tied) == 1:
        return tied[0]

    best_g, best_diff = tied[0], None
    for aid, r in album_candidates:
        g = genre_by_album.get(aid)
        if g not in tied or not duration or not r.get("duration"):
            continue
        diff = abs(r["duration"] - duration)
        if best_diff is None or diff < best_diff:
            best_diff, best_g = diff, g
    return best_g


def _genre_for_album(album_id):
    if album_id in _album_genre_cache:
        return _album_genre_cache[album_id]
    data = _http_json(DEEZER_ALBUM.format(album_id))
    genres = []
    if data and data.get("genres", {}).get("data"):
        genres = [g["name"] for g in data["genres"]["data"] if g.get("name") and g["name"] != "All"]
    result = genres[0] if genres else None
    _album_genre_cache[album_id] = result
    return result


def _write_file_genre(fpath, genre):
    lower = fpath.lower()
    try:
        if lower.endswith(".flac"):
            from mutagen.flac import FLAC
            audio = FLAC(fpath)
            audio["genre"] = [genre]
            audio.save()
        elif lower.endswith(".mp3"):
            from mutagen.id3 import ID3NoHeaderError
            from mutagen.easyid3 import EasyID3
            try:
                audio = EasyID3(fpath)
            except ID3NoHeaderError:
                audio = mutagen.File(fpath, easy=True)
                audio.add_tags()
            audio["genre"] = [genre]
            audio.save()
        elif lower.endswith(".m4a"):
            from mutagen.mp4 import MP4
            audio = MP4(fpath)
            audio["\xa9gen"] = [genre]
            audio.save()
        else:
            return False
        return True
    except Exception:
        return False


def fill_missing_genres(progress_cb=None, track_ids=None):
    """Scans tracks (all of them, or just `track_ids` if given) missing a
    genre and fills it in from Deezer. Scoping to specific tracks still
    only ever touches ones that are actually missing a genre -- correcting
    an existing-but-wrong genre is a different tool (unify_artist_genre.py)."""
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        raise RuntimeError(f"Music directory not set or not found: {MUSIC_DIR}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if track_ids:
        placeholders = ",".join("?" * len(track_ids))
        rows = conn.execute(
            f"SELECT id, path, artist, title, duration FROM tracks "
            f"WHERE id IN ({placeholders}) AND (primary_genre IS NULL OR primary_genre = '')",
            track_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, path, artist, title, duration FROM tracks "
            "WHERE primary_genre IS NULL OR primary_genre = ''"
        ).fetchall()

    found = not_found = errors = 0
    if progress_cb:
        progress_cb(0, len(rows), 0)

    for i, row in enumerate(rows):
        try:
            safe_artist = (row["artist"] or "").replace('"', "")
            safe_title = _search_title(row["title"]).replace('"', "")
            results = _http_json(DEEZER_SEARCH, {
                "q": f'artist:"{safe_artist}" track:"{safe_title}"',
                "limit": 5,
            })
            candidates = (results or {}).get("data") or []
            genre = _pick_genre(candidates, row["artist"], row["duration"])

            if genre:
                fpath = os.path.join(MUSIC_DIR, row["path"])
                if os.path.isfile(fpath) and _write_file_genre(fpath, genre):
                    conn.execute(
                        "UPDATE tracks SET genre=?, primary_genre=? WHERE id=?",
                        (genre, genre, row["id"]),
                    )
                    conn.commit()
                    found += 1
                else:
                    errors += 1
            else:
                not_found += 1
        except Exception:
            errors += 1

        if progress_cb and (i + 1) % 25 == 0:
            progress_cb(i + 1, len(rows), found)

        time.sleep(0.15)  # be polite to Deezer's public API

    conn.close()
    return {"checked": len(rows), "found": found, "not_found": not_found, "errors": errors}


if __name__ == "__main__":
    def _print_progress(done, total, found):
        print(f"  ... {done}/{total} checked, {found} filled so far", file=sys.stderr)

    stats = fill_missing_genres(progress_cb=_print_progress)
    print(stats)
