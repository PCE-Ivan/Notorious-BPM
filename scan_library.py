#!/usr/bin/env python3
"""Scans the music library directory and (re)builds the SQLite index used by app.py.

Run this once at first, and re-run any time files are added/removed/renamed.
"""
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import mutagen
from langdetect import detect_langs, DetectorFactory, LangDetectException

import config as jukebox_config

DetectorFactory.seed = 0

MUSIC_DIR = jukebox_config.get_music_dir()
DEFAULT_DB_PATH = os.path.join(jukebox_config.get_app_data_dir(), "library.db")
DB_PATH = os.environ.get("JUKEBOX_DB_PATH", DEFAULT_DB_PATH)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
EXTS = (".mp3", ".flac", ".m4a", ".wav", ".ogg")
SCAN_WORKERS = 8  # tag reads are I/O-bound (open + parse a header), not CPU-bound


def _is_real_audio_file(fname):
    """False for AppleDouble sidecar files (macOS writes a "._Song.mp3"
    next to every "Song.mp3" on any non-HFS/APFS filesystem -- FAT32/exFAT
    drives, network shares -- to hold the resource fork/xattrs it can't
    store natively). These match EXTS just like the real file, but aren't
    audio at all -- mutagen fails to parse them (logged as "could not
    read"), which was previously just silent wasted work and log noise on
    every scan of a drive with any of these on it."""
    return not fname.startswith("._")

YEAR_RE = re.compile(r"(\d{4})")


def parse_year(tags):
    for key in ("originaldate", "originalyear", "date", "year"):
        vals = tags.get(key)
        if not vals:
            continue
        m = YEAR_RE.search(str(vals[0]))
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                return y
    return None


def parse_bpm(tags):
    vals = tags.get("bpm")
    if not vals:
        return None
    try:
        return float(str(vals[0]).split()[0])
    except (ValueError, IndexError):
        return None


def first_or_none(tags, key):
    vals = tags.get(key)
    if not vals:
        return None
    return str(vals[0]).strip() or None


# Deemix downloads carry no language tag at all, so language is guessed from
# the track's own text: a reliable Unicode-script check catches non-Latin
# scripts outright (bucketed as "Other"), and a confidence-gated statistical
# guess separates Croatian/Spanish/English -- anything short, ambiguous, or
# a different language entirely defaults to English or Other. Only four
# buckets are exposed (Croatian, English, Spanish, Other) since finer-grained
# labels get noisy on short track titles. It's a best-effort label, not a
# ground truth.
_NON_LATIN_RANGES = (
    (0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xAC00, 0xD7A3),  # CJK
    (0x0600, 0x06FF),  # Arabic
    (0x0400, 0x04FF),  # Cyrillic
    (0x0370, 0x03FF),  # Greek
    (0x0590, 0x05FF),  # Hebrew
    (0x0E00, 0x0E7F),  # Thai
    (0x0900, 0x097F),  # Devanagari
)


def _has_non_latin_script(text):
    if not text:
        return False
    return any(lo <= ord(c) <= hi for c in text for lo, hi in _NON_LATIN_RANGES)


def detect_language(title, artist, album):
    combined = " ".join(filter(None, [title, album])).strip()
    if _has_non_latin_script(combined) or _has_non_latin_script(artist or ""):
        return "Other"

    if len(combined) < 12:
        return "English"
    try:
        top = detect_langs(combined)[0]
    except LangDetectException:
        return "English"
    if top.prob >= 0.98:
        return {
            "es": "Spanish",
            "hr": "Croatian",
            "fr": "French",
            "it": "Italian",
            "en": "English",
        }.get(top.lang, "Other")
    return "English"


def build_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            artist TEXT,
            album TEXT,
            title TEXT,
            genre TEXT,
            primary_genre TEXT,
            year INTEGER,
            decade INTEGER,
            bpm REAL,
            duration REAL,
            ext TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
        CREATE INDEX IF NOT EXISTS idx_tracks_genre ON tracks(primary_genre);
        CREATE INDEX IF NOT EXISTS idx_tracks_decade ON tracks(decade);

        CREATE TABLE IF NOT EXISTS ratings (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
            rating INTEGER NOT NULL,
            rated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
            track_id INTEGER REFERENCES tracks(id) ON DELETE CASCADE,
            position INTEGER,
            PRIMARY KEY (playlist_id, track_id)
        );
        """
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tracks)").fetchall()]
    if "language" not in cols:
        conn.execute("ALTER TABLE tracks ADD COLUMN language TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_language ON tracks(language)")
    conn.commit()


def scan(progress_cb=None, force_prune=False):
    """progress_cb(done, total), called after every file -- lets a caller
    (app.py's /api/choose-folder and /api/rescan, both of which used to
    block silently for the whole scan with zero feedback) show live
    progress instead of just looking hung on a large library. `total` comes
    from a first, cheap filename-only walk (no mutagen/tag reads) before
    the real pass, so it's a real count from the start rather than growing
    as files are found.

    force_prune=True bypasses the >20%-removal safety guard below for this
    one explicit run -- for a caller that's already shown the user the
    guard's warning and gotten explicit confirmation this is expected
    (e.g. a real reorganization), not something to ever default to."""
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        raise RuntimeError(f"Music directory not set or not found: {MUSIC_DIR}")

    conn = sqlite3.connect(DB_PATH)
    build_schema(conn)
    cur = conn.cursor()

    cur.execute("SELECT path FROM tracks")
    existing_paths = {row[0] for row in cur.fetchall()}
    seen_paths = set()

    candidates = []  # (fpath, rel, root, fname)
    for root, _dirs, files in os.walk(MUSIC_DIR):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in EXTS or not _is_real_audio_file(fname):
                continue
            fpath = os.path.join(root, fname)
            candidates.append((fpath, os.path.relpath(fpath, MUSIC_DIR), root, fname))
    total = len(candidates)

    inserted = 0
    updated = 0
    skipped = 0
    done = 0
    t0 = time.time()

    def _extract(candidate):
        """Runs in a worker thread: opens and parses one file's tags, pure
        function with no database access -- every actual sqlite write
        still happens on the main thread below, serialized the same as
        before. Returns None for a file that couldn't be read (logged
        here since that's the same for every caller), or a dict of the
        fields the caller writes to the tracks table."""
        fpath, rel, root, fname = candidate
        ext = os.path.splitext(fname)[1].lower()
        try:
            audio = mutagen.File(fpath, easy=True)
        except Exception as e:
            print(f"  ! could not read {rel}: {e}", file=sys.stderr)
            return None
        if audio is None:
            return None

        tags = dict(audio.tags) if audio.tags else {}
        artist = first_or_none(tags, "artist") or os.path.basename(root)
        album = first_or_none(tags, "album")
        title = first_or_none(tags, "title") or os.path.splitext(fname)[0]
        # A multi-genre ID3 tag can arrive as a single list item with its
        # values joined by NUL bytes (e.g. "Pop\x00Rock") rather than as
        # separate list entries -- split those out before building the
        # genre fields, or primary_genre ends up as a distinct compound
        # value per genre combination instead of grouping under "Pop".
        genre_parts = []
        for g in tags.get("genre") or []:
            genre_parts.extend(str(g).split("\x00"))
        genre_parts = [g.strip() for g in genre_parts if g.strip()]
        seen_genres = set()
        genre_parts = [g for g in genre_parts if not (g in seen_genres or seen_genres.add(g))]
        genre = "; ".join(genre_parts) if genre_parts else None
        primary_genre = genre_parts[0] if genre_parts else None
        year = parse_year(tags)
        decade = (year // 10) * 10 if year else None
        bpm = parse_bpm(tags)
        duration = getattr(audio.info, "length", None) if audio.info else None
        language = detect_language(title, artist, album)

        return {
            "rel": rel, "ext": ext, "artist": artist, "album": album, "title": title,
            "genre": genre, "primary_genre": primary_genre, "year": year, "decade": decade,
            "bpm": bpm, "duration": duration, "language": language,
        }

    # The actual file open + tag parse is the slow part and fully
    # independent per file -- a small thread pool overlaps that I/O
    # instead of doing it strictly one file at a time. Every sqlite write
    # stays serialized on this one connection/thread either way, same as
    # before.
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(_extract, c): c for c in candidates}
        for future in as_completed(futures):
            fpath, rel, root, fname = futures[future]
            seen_paths.add(rel)
            done += 1
            if progress_cb and done % 10 == 0:
                progress_cb(done, total)

            fields = future.result()
            if fields is None:
                skipped += 1
                continue

            if rel in existing_paths:
                cur.execute(
                    """UPDATE tracks SET artist=?, album=?, title=?, genre=?, primary_genre=?,
                       year=?, decade=?, bpm=?, duration=?, ext=?, language=? WHERE path=?""",
                    (fields["artist"], fields["album"], fields["title"], fields["genre"],
                     fields["primary_genre"], fields["year"], fields["decade"], fields["bpm"],
                     fields["duration"], fields["ext"], fields["language"], fields["rel"]),
                )
                updated += 1
            else:
                cur.execute(
                    """INSERT INTO tracks (path, artist, album, title, genre, primary_genre,
                       year, decade, bpm, duration, ext, language) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (fields["rel"], fields["artist"], fields["album"], fields["title"], fields["genre"],
                     fields["primary_genre"], fields["year"], fields["decade"], fields["bpm"],
                     fields["duration"], fields["ext"], fields["language"]),
                )
                inserted += 1

            if (inserted + updated) % 500 == 0:
                conn.commit()
                print(f"  ... {inserted + updated} processed ({time.time()-t0:.0f}s)")

    if progress_cb:
        progress_cb(done, total)

    removed_paths = existing_paths - seen_paths
    # A drive that's asleep, unmounted mid-scan, or just slow to respond can
    # make a huge chunk of otherwise-fine files look "missing" -- pruning
    # those would silently wipe them from the library. Refuse to remove more
    # than a fifth of the index (and require a meaningful minimum) in one
    # pass; a genuine mass-deletion of files on disk is rare enough that
    # requiring a second, explicit rescan to confirm is the safer default.
    suspicious = (
        not force_prune
        and bool(existing_paths)
        and len(removed_paths) > max(50, len(existing_paths) * 0.2)
    )
    if suspicious:
        removed_paths = set()
    else:
        for rel in removed_paths:
            cur.execute("DELETE FROM tracks WHERE path=?", (rel,))

    conn.commit()
    conn.close()

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.0f}s: {inserted} new, {updated} updated, "
          f"{len(removed_paths)} removed, {skipped} skipped.")
    result = {
        "inserted": inserted,
        "updated": updated,
        "removed": len(removed_paths),
        "skipped": skipped,
        "elapsed": round(elapsed, 1),
    }
    if suspicious:
        result["warning"] = (
            f"{len(existing_paths) - len(seen_paths)} indexed files weren't found on disk this pass "
            "(more than 20% of the library) -- skipped removing them in case a drive was asleep or "
            "unmounted. Re-run once you've confirmed the drive is connected if they're genuinely gone."
        )
    return result


if __name__ == "__main__":
    try:
        scan()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
