#!/usr/bin/env python3
"""On-demand enrichment: fixes a track's year to its ORIGINAL release year
instead of whatever specific edition it was tagged from. deemix downloads
(and this library is almost entirely M4A/ALAC ones) never carry an
"original date" tag distinct from the release date of the specific edition
downloaded -- a track pulled from a "Remastered 2011" reissue or a
"Greatest Hits" compilation gets that edition's date, not the song's real
release year, and mutagen's easy-mode MP4 reader doesn't even expose an
original-date field to fall back to.

The fix: search Deezer (same catalog deemix pulls from) for every album
that contains a matching artist+track, and take the EARLIEST release date
found. Remasters, deluxe editions, and compilations are by definition
released after the original, so the earliest match across all of a song's
pressings is a solid proxy for "when it actually came out."
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

import mutagen

import config as jukebox_config

DEFAULT_DB_PATH = os.path.join(jukebox_config.get_app_data_dir(), "library.db")
DB_PATH = os.environ.get("JUKEBOX_DB_PATH", DEFAULT_DB_PATH)
MUSIC_DIR = jukebox_config.get_music_dir()

DEEZER_SEARCH = "https://api.deezer.com/search"
DEEZER_ALBUM = "https://api.deezer.com/album/{}"

_album_year_cache = {}

# Same fix as fill_genres.py: a bracketed edition qualifier -- "(Remastered)",
# "(Live)", "(2011 Remaster Version)" -- almost never appears in Deezer's own
# track title, so leaving it in the search query returns zero matches for
# exactly the tracks this lookup exists to correct.
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


def _release_year_for_album(album_id):
    if album_id in _album_year_cache:
        return _album_year_cache[album_id]
    data = _http_json(DEEZER_ALBUM.format(album_id))
    year = None
    if data and data.get("release_date"):
        try:
            y = int(str(data["release_date"])[:4])
            if 1900 <= y <= 2100:
                year = y
        except (ValueError, TypeError):
            year = None
    _album_year_cache[album_id] = year
    return year


def _earliest_year(candidates, artist):
    artist_lower = (artist or "").strip().lower()
    qualifying = [
        r for r in candidates
        if artist_lower and artist_lower in (r.get("artist", {}).get("name") or "").strip().lower()
    ] or candidates[:1]

    seen_albums = set()
    years = []
    for r in qualifying:
        aid = r.get("album", {}).get("id")
        if aid and aid not in seen_albums:
            seen_albums.add(aid)
            y = _release_year_for_album(aid)
            if y:
                years.append(y)
    return min(years) if years else None


def _write_file_year(fpath, year):
    lower = fpath.lower()
    try:
        if lower.endswith(".flac"):
            from mutagen.flac import FLAC
            audio = FLAC(fpath)
            audio["date"] = [str(year)]
            audio["originaldate"] = [str(year)]
            audio.save()
        elif lower.endswith(".mp3"):
            from mutagen.id3 import ID3NoHeaderError
            from mutagen.easyid3 import EasyID3
            try:
                audio = EasyID3(fpath)
            except ID3NoHeaderError:
                audio = mutagen.File(fpath, easy=True)
                audio.add_tags()
            audio["date"] = [str(year)]
            audio["originaldate"] = [str(year)]
            audio.save()
        elif lower.endswith(".m4a"):
            from mutagen.mp4 import MP4
            audio = MP4(fpath)
            audio["\xa9day"] = [str(year)]
            audio.save()
        else:
            return False
        return True
    except Exception:
        return False


def fix_release_years(progress_cb=None, track_ids=None):
    """Scans tracks (all of them, or just `track_ids` if given) and updates
    the year/tag whenever Deezer's earliest matching release predates what's
    currently stored -- so a track never gets bumped to a *later* date, only
    corrected toward an earlier, more "original" one."""
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        raise RuntimeError(f"Music directory not set or not found: {MUSIC_DIR}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if track_ids:
        placeholders = ",".join("?" * len(track_ids))
        rows = conn.execute(
            f"SELECT id, path, artist, title, year FROM tracks WHERE id IN ({placeholders})", track_ids
        ).fetchall()
    else:
        rows = conn.execute("SELECT id, path, artist, title, year FROM tracks").fetchall()

    updated = unchanged = not_found = errors = 0
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
            earliest = _earliest_year(candidates, row["artist"])

            current = row["year"]
            if earliest and (current is None or earliest < current):
                fpath = os.path.join(MUSIC_DIR, row["path"])
                if os.path.isfile(fpath) and _write_file_year(fpath, earliest):
                    decade = (earliest // 10) * 10
                    conn.execute("UPDATE tracks SET year=?, decade=? WHERE id=?", (earliest, decade, row["id"]))
                    conn.commit()
                    updated += 1
                else:
                    errors += 1
            elif earliest:
                unchanged += 1
            else:
                not_found += 1
        except Exception:
            errors += 1

        if progress_cb and (i + 1) % 25 == 0:
            progress_cb(i + 1, len(rows), updated)

        time.sleep(0.15)  # be polite to Deezer's public API

    conn.close()
    return {"checked": len(rows), "updated": updated, "unchanged": unchanged,
            "not_found": not_found, "errors": errors}


if __name__ == "__main__":
    def _print_progress(done, total, updated):
        print(f"  ... {done}/{total} checked, {updated} corrected so far", file=sys.stderr)

    stats = fix_release_years(progress_cb=_print_progress)
    print(stats)
