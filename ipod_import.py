"""Reads an iPod Classic's own iTunesDB to recover real filenames for its
obfuscated on-disk tracks (iPod_Control/Music/F00/XXXX.m4a etc.), then
copies the audio files out under real "Artist - Title" names. Classic
(clickwheel) iPods only -- they mount as a plain USB/FireWire disk and
store ordinary, DRM-free MP3/AAC/ALAC files (Apple dropped FairPlay Music
DRM in 2009), unlike an iPod Touch, which exposes no such filesystem.

iTunesDB is a proprietary but long-reverse-engineered binary format: a tree
of self-describing chunks (each with its own header_length/total_length, so
a chunk can always be skipped over even without knowing every field inside
it) rooted at one "mhbd". This only walks the parts needed to recover a
file's real name -- the audio files themselves still carry their original
ID3/MP4 tags untouched, which is what scan_library.py reads once the copy
lands in the configured music folder, so this module never needs to (and
deliberately doesn't try to) recover every metadata field mhit encodes.
"""
import os
import shutil
import struct

AUDIO_EXTS = {".mp3", ".m4a", ".m4b", ".aac", ".wav", ".aif", ".aiff", ".alac"}

# mhod content-type codes that matter here; see the module docstring.
MHOD_TITLE = 1
MHOD_PATH = 2
MHOD_ALBUM = 3
MHOD_ARTIST = 4
MHOD_GENRE = 5


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def find_ipod():
    """Scans /Volumes for a mounted iPod Classic (anything exposing
    iPod_Control/iTunes/iTunesDB). Returns None if none is connected."""
    volumes_root = "/Volumes"
    if not os.path.isdir(volumes_root):
        return None
    for name in sorted(os.listdir(volumes_root)):
        mount = os.path.join(volumes_root, name)
        itunesdb = os.path.join(mount, "iPod_Control", "iTunes", "iTunesDB")
        if os.path.isfile(itunesdb):
            try:
                tracks = parse_itunesdb(itunesdb, mount)
            except Exception:
                continue
            return {"name": name, "mount": mount, "itunesdb_path": itunesdb, "track_count": len(tracks)}
    return None


def parse_itunesdb(itunesdb_path, mount):
    """Walks mhbd -> the tracks mhsd (type 1) -> mhlt -> each mhit's mhod
    children, and returns a list of {title, artist, album, genre, real_path}
    dicts, one per track whose referenced file actually exists on disk.
    Every chunk is skipped via its own total_length, so this stays correct
    even for chunk-internal fields this never looks at."""
    with open(itunesdb_path, "rb") as f:
        data = f.read()

    if data[0:4] != b"mhbd":
        raise ValueError("Not an iTunesDB file (missing mhbd header)")

    mhbd_hdr_len = _u32(data, 4)
    num_children = _u32(data, 20)

    tracks_mhsd_off = None
    off = mhbd_hdr_len
    for _ in range(num_children):
        if data[off:off + 4] != b"mhsd":
            break
        total_len = _u32(data, off + 8)
        chunk_type = _u32(data, off + 12)
        if chunk_type == 1:
            tracks_mhsd_off = off
            break
        off += total_len
    if tracks_mhsd_off is None:
        return []

    mhsd_hdr_len = _u32(data, tracks_mhsd_off + 4)
    mhlt_off = tracks_mhsd_off + mhsd_hdr_len
    if data[mhlt_off:mhlt_off + 4] != b"mhlt":
        return []
    mhlt_hdr_len = _u32(data, mhlt_off + 4)
    num_tracks = _u32(data, mhlt_off + 8)

    tracks = []
    off = mhlt_off + mhlt_hdr_len
    for _ in range(num_tracks):
        if data[off:off + 4] != b"mhit":
            break
        mhit_total_len = _u32(data, off + 8)
        mhit_hdr_len = _u32(data, off + 4)
        num_mhods = _u32(data, off + 12)

        fields = {}
        mhod_off = off + mhit_hdr_len
        for _ in range(num_mhods):
            if data[mhod_off:mhod_off + 4] != b"mhod":
                break
            mhod_total_len = _u32(data, mhod_off + 8)
            mhod_type = _u32(data, mhod_off + 12)
            str_len = _u32(data, mhod_off + 28)
            if mhod_type in (MHOD_TITLE, MHOD_PATH, MHOD_ALBUM, MHOD_ARTIST, MHOD_GENRE) and str_len:
                raw = data[mhod_off + 40:mhod_off + 40 + str_len]
                try:
                    fields[mhod_type] = raw.decode("utf-16-le")
                except UnicodeDecodeError:
                    pass
            mhod_off += mhod_total_len

        path = fields.get(MHOD_PATH)
        if path:
            # iTunesDB paths use ':' as a separator (classic Mac OS
            # convention) and start with one, e.g. ":iPod_Control:Music:F08:CXWW.m4a".
            rel = path.replace(":", "/").lstrip("/")
            real_path = os.path.join(mount, rel)
            if os.path.isfile(real_path) and os.path.splitext(real_path)[1].lower() in AUDIO_EXTS:
                tracks.append({
                    "title": fields.get(MHOD_TITLE) or os.path.splitext(os.path.basename(real_path))[0],
                    "artist": fields.get(MHOD_ARTIST) or "",
                    "album": fields.get(MHOD_ALBUM) or "",
                    "genre": fields.get(MHOD_GENRE) or "",
                    "real_path": real_path,
                })

        off += mhit_total_len

    return tracks


# organize_by_artist.py already enforces music_dir/<Artist>/<file> as the
# library's own on-disk layout (see its module docstring) -- reusing its
# exact artist-folder sanitization/collision helpers instead of rolling
# separate ones here means an iPod import lands in a shape indistinguishable
# from anything else in the library, not a lookalike with its own rules.
import organize_by_artist

_INVALID_CHARS_RE = organize_by_artist._INVALID_CHARS_RE


def _safe_component(s, fallback):
    s = _INVALID_CHARS_RE.sub("", (s or "")).strip().rstrip(". ")
    return s or fallback


def _default_normalize_key(artist, title):
    """Fallback (artist, title) normalization for standalone use/testing.
    app.py instead passes its own _normalize_dup_artist/_normalize_dup_title
    (see the Duplicates feature) so "already in the library" here means
    exactly what it means there -- including treating "Song (Live)" and
    "Song" as the same song."""
    return ((artist or "").strip().lower(), (title or "").strip().lower())


# Excluded from scan_library.py's and organize_by_artist.py's own directory
# walks (one line added to each -- see those files) so a batch sitting here
# unreviewed never leaks into the real library through an incidental
# Rescan/Organize. Dot-prefixed so it also reads as "not a real artist
# folder" to a human glancing at the music folder in Finder.
STAGING_DIRNAME = ".ipod_staging"

# Written into a batch's staging_root only once import_tracks has walked
# every track it found in the iTunesDB without an unhandled error -- its
# *absence* is what tells a batch that's genuinely done and waiting for
# review apart from one where the copy was interrupted partway (e.g. a real
# Classic iPod dropping off USB mid-copy: `OSError: [Errno 6] Device not
# configured`, confirmed happening in practice, with the iPod itself still
# mounted and healthy afterward) and, on disk, looks identical to a
# finished batch otherwise. See is_batch_complete/import_tracks below.
COMPLETE_MARKER = ".import_complete"


def staging_root_for(music_dir, ipod_name):
    return os.path.join(music_dir, STAGING_DIRNAME, _safe_component(ipod_name, "iPod"))


def is_batch_complete(staging_root):
    return os.path.isfile(os.path.join(staging_root, COMPLETE_MARKER))


def list_pending_batches(music_dir):
    """Every staged-but-not-yet-moved batch currently sitting on disk, by
    folder name under STAGING_DIRNAME -- lets app.py offer to resume a
    review instead of silently starting a second overlapping import, and
    lets it find "the" batch to act on when a caller doesn't say which one
    (there's normally at most one). `complete` tells app.py whether this is
    a finished batch simply waiting for review, or one where copying was
    interrupted and needs to be re-run against the same staging_root before
    review can be trusted to show the whole batch."""
    root = os.path.join(music_dir, STAGING_DIRNAME)
    if not os.path.isdir(root):
        return []
    batches = []
    for name in sorted(os.listdir(root)):
        staging_root = os.path.join(root, name)
        if not os.path.isdir(staging_root):
            continue
        count = len(_staged_file_paths(staging_root))
        if count:
            batches.append({"name": name, "count": count, "complete": is_batch_complete(staging_root)})
    return batches


def _staged_file_paths(staging_root):
    from scan_library import _is_real_audio_file

    paths = []
    for root, _dirs, files in os.walk(staging_root):
        for fname in files:
            # scan_library.py already solved this exact problem for its
            # own library walk: on any non-HFS/APFS filesystem (this
            # staging root lives under MUSIC_DIR, which for an external
            # FAT32/exFAT drive very much qualifies), macOS writes a
            # "._Song.m4a" AppleDouble sidecar next to every real file to
            # hold what it can't store natively. It matches AUDIO_EXTS
            # just like the real file -- without this check, every staged
            # count and move silently doubles, and mutagen/shutil choke on
            # these non-audio stand-ins (confirmed against a real import:
            # move_staged_to_library errored out on one mid-batch).
            if os.path.splitext(fname)[1].lower() in AUDIO_EXTS and _is_real_audio_file(fname):
                paths.append(os.path.join(root, fname))
    return paths


def _natural_staged_path(staging_root, artist, title, ext):
    artist_label = _safe_component(artist, "Unknown Artist")
    title_label = _safe_component(title, "Untitled")
    dest_folder = os.path.join(staging_root, artist_label)
    return dest_folder, os.path.join(dest_folder, f"{artist_label} - {title_label}{ext}")


def _makedirs_matching_case(dest_folder):
    """Like os.makedirs(dest_folder, exist_ok=True), but also fixes the
    folder's case if it already exists under a different one. On macOS's
    default case-insensitive-but-case-preserving filesystem, exist_ok=True
    is a silent no-op when the folder is already there under another case
    -- so once an artist folder is first created (e.g. from a track tagged
    "Adam & the Ants"), a later correction to "Adam & The Ants" renames the
    *file* but leaves the folder stuck at its original case forever. Used
    at the two call sites (_refile_staged, move_staged_to_library) where a
    name correction can produce a different case for a folder that may
    already exist; import_tracks's own initial copy has no "corrected"
    case to prefer, so it's left as a plain makedirs."""
    parent = os.path.dirname(dest_folder)
    wanted_name = os.path.basename(dest_folder)
    try:
        entries = os.listdir(parent)
    except OSError:
        entries = []
    for entry in entries:
        if entry == wanted_name:
            return
        if entry.lower() == wanted_name.lower():
            tmp_path = os.path.join(parent, wanted_name + ".__caserename__")
            os.rename(os.path.join(parent, entry), tmp_path)
            os.rename(tmp_path, dest_folder)
            return
    os.makedirs(dest_folder, exist_ok=True)


def _apparent_bitrate_kbps(fpath, duration):
    """File size / duration, in kbps -- cheap to compute (no decoding), and
    the same proxy app.py's own duplicate-quality comparison
    (_resolve_same_recording) already uses for exactly this "which copy is
    actually better" question. Not exact for VBR, but good enough to tell
    a 128kbps rip from a lossless one, which is the actual decision this
    is for."""
    if not duration:
        return None
    try:
        size = os.path.getsize(fpath)
    except OSError:
        return None
    return round(size * 8 / duration / 1000)


def _read_duration(fpath):
    import mutagen
    try:
        audio = mutagen.File(fpath)
    except Exception:
        return None
    return getattr(audio.info, "length", None) if audio and audio.info else None


def import_tracks(mount, staging_root, music_dir, existing_index=None, normalize_key=None, progress_cb=None):
    """Copies every track this can resolve into staging_root, laid out
    exactly like the real library will eventually see it once moved there
    (see move_staged_to_library below): staging_root/<Artist>/<Artist> -
    <Title>.ext. A destination file that already exists at exactly the
    source's size is treated as already staged from a previous run of this
    same import and left alone; anything else occupying that exact name
    gets a "(2)", "(3)", ... suffix, the same convention
    organize_by_artist.py itself uses. Source files on the iPod are only
    ever read, never modified or deleted.

    `existing_index` -- {(normalized_artist, normalized_title): {"path":
    relpath, "duration": seconds}} for the real library at the moment the
    import starts -- is checked *before* copying: a match is treated as a
    duplicate and is never staged at all, only reported in the returned
    `duplicates` list, alongside an apparent-bitrate comparison of the two
    copies so the caller can see whether the iPod's version is actually
    better or worse quality before accepting the skip. This is separate
    from (and runs before) move_staged_to_library's own duplicate check,
    which only ever reports -- that one exists to catch anything that
    still matches at move time, such as two tracks within this same batch
    that happen to be the same song."""
    existing_index = existing_index or {}
    normalize_key = normalize_key or _default_normalize_key
    itunesdb_path = os.path.join(mount, "iPod_Control", "iTunes", "iTunesDB")
    tracks = parse_itunesdb(itunesdb_path, mount)
    os.makedirs(staging_root, exist_ok=True)
    # Cleared up front and only written back at the very end (see
    # COMPLETE_MARKER above) -- a re-run against a staging_root left over
    # from an interrupted previous attempt must not still look "done" to
    # is_batch_complete() while *this* run is itself in progress.
    marker_path = os.path.join(staging_root, COMPLETE_MARKER)
    if os.path.exists(marker_path):
        os.remove(marker_path)

    copied_paths = []
    already_staged = 0
    duplicates = []
    total = len(tracks)
    for i, t in enumerate(tracks):
        existing = existing_index.get(normalize_key(t["artist"], t["title"]))
        if existing is not None:
            duplicates.append({
                "artist": t["artist"],
                "title": t["title"],
                "library_path": existing["path"],
                "ipod_bitrate": _apparent_bitrate_kbps(t["real_path"], _read_duration(t["real_path"])),
                "library_bitrate": _apparent_bitrate_kbps(os.path.join(music_dir, existing["path"]), existing.get("duration")),
            })
            if progress_cb:
                progress_cb(i + 1, total)
            continue

        ext = os.path.splitext(t["real_path"])[1].lower()
        dest_folder, natural_path = _natural_staged_path(staging_root, t["artist"], t["title"], ext)

        src_size = os.path.getsize(t["real_path"])
        if os.path.isfile(natural_path) and os.path.getsize(natural_path) == src_size:
            already_staged += 1
        else:
            os.makedirs(dest_folder, exist_ok=True)
            dest_path = (
                organize_by_artist._unique_dest_path(natural_path)
                if os.path.exists(natural_path) else natural_path
            )
            shutil.copy2(t["real_path"], dest_path)
            copied_paths.append(os.path.relpath(dest_path, staging_root))

        if progress_cb:
            progress_cb(i + 1, total)

    # Reached only once every track has been accounted for (copied, already
    # staged, or a reported duplicate) with no exception along the way --
    # an interruption partway through (see COMPLETE_MARKER above) leaves
    # this unwritten, which is exactly the signal list_pending_batches and
    # app.py need.
    with open(marker_path, "w"):
        pass

    return {
        "total": total,
        "copied": len(copied_paths),
        "already_staged": already_staged,
        "copied_paths": copied_paths,
        "duplicates": duplicates,
    }


# ---------------------------------------------------------- review + fix --
# Staged tracks are never inserted into the real tracks table -- reusing it
# with a "pending" flag was the other option, but ~44 separate FROM tracks
# queries across app.py would each need auditing for that flag, with real
# risk of missing one and leaking unreviewed content into search/stats/
# playlists. Reading tags straight off the staged files (mutagen) for the
# review list, and writing corrections straight back to those files,
# avoids that entirely and touches none of those existing queries.
def _read_tags_for_review(fpath):
    """(artist, title, album, genre, year, duration, bitrate, ext, has_art)
    read straight from a file's own current tags -- no DB row exists yet
    for a staged track, so this is the only source of truth, and it's
    re-read fresh every time rather than cached, since an earlier fix step
    in the same review session may have just changed these tags. Falls
    back to the containing folder/filename for artist/title on a
    completely untagged file, matching how scan_library.py treats one for
    the real library."""
    import mutagen
    from scan_library import first_or_none, parse_year

    try:
        audio = mutagen.File(fpath, easy=True)
    except Exception:
        return None
    if audio is None:
        return None

    tags = dict(audio.tags) if audio.tags else {}
    artist = first_or_none(tags, "artist") or os.path.basename(os.path.dirname(fpath))
    album = first_or_none(tags, "album")
    title = first_or_none(tags, "title") or os.path.splitext(os.path.basename(fpath))[0]
    genre_parts = []
    for g in tags.get("genre") or []:
        genre_parts.extend(str(g).split("\x00"))
    genre = "; ".join(g.strip() for g in genre_parts if g.strip()) or None
    year = parse_year(tags)
    duration = getattr(audio.info, "length", None) if audio.info else None

    return {
        "artist": artist, "title": title, "album": album, "genre": genre,
        "year": year, "duration": duration, "bitrate": _apparent_bitrate_kbps(fpath, duration),
        "ext": os.path.splitext(fpath)[1].lower(),
        "has_art": _has_embedded_art(fpath),
    }


def _has_embedded_art(fpath):
    """Same per-format check as app.py's _read_raw_tag_presence, just for
    art alone -- kept independent (not imported from app.py) since app.py
    already imports this module and a cycle back the other way would be
    fragile for no real benefit."""
    lower = fpath.lower()
    try:
        if lower.endswith(".flac"):
            from mutagen.flac import FLAC
            return bool(FLAC(fpath).pictures)
        if lower.endswith(".mp3"):
            from mutagen.id3 import ID3
            return bool(ID3(fpath).getall("APIC"))
        if lower.endswith((".m4a", ".m4b", ".alac")):
            from mutagen.mp4 import MP4
            audio = MP4(fpath)
            return bool(audio.tags and audio.tags.get("covr"))
    except Exception:
        pass
    return True  # unrecognized/unreadable -- never report a false "missing"


def _embed_art(fpath, image_bytes):
    """Writes cover art directly into the file's own tag -- matching how
    the rest of the library already carries art (embedded, not a sidecar
    file), so a track looks identical to any other once moved out of
    staging. Mirrors the per-format branches _write_file_genre etc. use
    elsewhere in this codebase."""
    lower = fpath.lower()
    try:
        if lower.endswith(".flac"):
            from mutagen.flac import FLAC, Picture
            audio = FLAC(fpath)
            pic = Picture()
            pic.data = image_bytes
            pic.type = 3
            pic.mime = "image/jpeg"
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()
        elif lower.endswith(".mp3"):
            from mutagen.id3 import ID3, ID3NoHeaderError, APIC
            try:
                audio = ID3(fpath)
            except ID3NoHeaderError:
                audio = ID3()
            audio.delall("APIC")
            audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=image_bytes))
            audio.save(fpath)
        elif lower.endswith((".m4a", ".m4b", ".alac")):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(fpath)
            audio["covr"] = [MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
        else:
            return False
        return True
    except Exception:
        return False


def list_staged_tracks(staging_root):
    """The review screen's data source -- see _read_tags_for_review above
    for why this reads files directly instead of querying a DB."""
    tracks = []
    for fpath in _staged_file_paths(staging_root):
        info = _read_tags_for_review(fpath)
        if info:
            info["path"] = os.path.relpath(fpath, staging_root)
            tracks.append(info)
    tracks.sort(key=lambda t: ((t["artist"] or "").lower(), (t["title"] or "").lower()))
    return tracks


def _refile_staged(staging_root, fpath, artist, title):
    """After a name correction, moves the file to match its (possibly new)
    tags -- keeps the staging area laid out the same way import_tracks()
    already lays it out, so what the review screen shows always matches
    where the file actually lives.

    macOS's default filesystem is case-insensitive but case-preserving --
    renaming "...on tv.m4a" to "...On TV.m4a" (exactly what a real Deezer
    capitalization fix does) makes os.path.exists(natural_path) true
    before the move even happens, since as far as the filesystem is
    concerned that path already exists (it's the same file). A naive
    exists-check here would misread that as "something else already has
    this name" and give the file a spurious "(2)" suffix on every single
    case-only correction. os.path.samefile tells the two cases apart."""
    ext = os.path.splitext(fpath)[1].lower()
    dest_folder, natural_path = _natural_staged_path(staging_root, artist, title, ext)
    if natural_path == fpath:
        return fpath
    _makedirs_matching_case(dest_folder)
    if os.path.exists(natural_path) and not os.path.samefile(fpath, natural_path):
        dest_path = organize_by_artist._unique_dest_path(natural_path)
    else:
        dest_path = natural_path
    shutil.move(fpath, dest_path)
    return dest_path


def fix_staged_names(staging_root, progress_cb=None):
    """Same Deezer-backed artist/title correction "Fix artist & track
    names" already runs against the real library (fix_artist_title.
    _lookup_correction), applied directly to staged files instead of DB
    rows -- there's no DB row for a staged track yet. Renames/refolders
    the file to match afterward so the staging area stays consistent with
    whatever the file's tags now say."""
    import time
    import fix_artist_title

    paths = _staged_file_paths(staging_root)
    total = len(paths)
    fixed = 0
    for i, fpath in enumerate(paths):
        try:
            info = _read_tags_for_review(fpath)
            if info:
                correction = fix_artist_title._lookup_correction(info)
                if correction:
                    new_artist, new_title = correction
                    if fix_artist_title._write_file_artist_title(fpath, new_artist, new_title):
                        _refile_staged(staging_root, fpath, new_artist, new_title)
                        fixed += 1
        except Exception:
            pass
        if progress_cb:
            progress_cb(i + 1, total)
        time.sleep(0.15)  # be polite to Deezer's public API -- see fill_genres.py
    return {"checked": total, "fixed": fixed}


def fix_staged_tags(staging_root, progress_cb=None):
    """Fills missing genre/year via Deezer (fill_genres/fill_years' own
    per-track lookups, reused directly), one shared search per track
    covering both -- same reasoning as fix_staged_names above for why this
    writes straight to the file rather than a DB row."""
    import time
    import fill_genres
    import fill_years

    paths = _staged_file_paths(staging_root)
    total = len(paths)
    fixed_genre = fixed_year = 0
    for i, fpath in enumerate(paths):
        try:
            info = _read_tags_for_review(fpath)
            if info and (not info.get("genre") or not info.get("year")):
                safe_artist = (info["artist"] or "").replace('"', "")
                safe_title = fill_genres._search_title(info["title"] or "").replace('"', "")
                results = fill_genres._http_json(fill_genres.DEEZER_SEARCH, {
                    "q": f"{safe_artist} {safe_title}".strip(), "limit": 5,
                })
                candidates = (results or {}).get("data") or []
                if not info.get("genre"):
                    genre = fill_genres._pick_genre(candidates, info["artist"], info.get("duration"))
                    if genre and fill_genres._write_file_genre(fpath, genre):
                        fixed_genre += 1
                if not info.get("year"):
                    earliest = fill_years._earliest_year(candidates, info["artist"])
                    if earliest and fill_years._write_file_year(fpath, earliest):
                        fixed_year += 1
        except Exception:
            pass
        if progress_cb:
            progress_cb(i + 1, total)
        time.sleep(0.15)  # be polite to Deezer's public API -- see fill_genres.py
    return {"checked": total, "fixed_genre": fixed_genre, "fixed_year": fixed_year}


def fix_staged_art(staging_root, progress_cb=None):
    """Best-effort Deezer cover-art backfill for staged tracks missing
    embedded art -- iPod syncs frequently drop full-size art to save
    device space even when the source library had it. Same
    same-artist-candidate filtering app.py's _fetch_and_cache_art uses for
    the real library's per-track "Fetch cover art" button, reused here
    against files instead of a DB row + ART_CACHE_DIR."""
    import time
    import urllib.request
    import fill_genres

    paths = [p for p in _staged_file_paths(staging_root) if not _has_embedded_art(p)]
    total = len(paths)
    fixed = 0
    for i, fpath in enumerate(paths):
        try:
            info = _read_tags_for_review(fpath)
            if info:
                results = fill_genres._http_json(fill_genres.DEEZER_SEARCH, {
                    "q": f'{(info["artist"] or "").strip()} {(info["title"] or "").strip()}'.strip(), "limit": 5,
                })
                candidates = (results or {}).get("data") or []
                artist_lower = (info["artist"] or "").strip().lower()
                same_artist = [
                    c for c in candidates
                    if artist_lower and artist_lower in (c.get("artist", {}).get("name") or "").strip().lower()
                ]
                best = (same_artist or candidates or [None])[0]
                cover_url = best.get("album", {}).get("cover_big") if best else None
                if cover_url:
                    with urllib.request.urlopen(cover_url, timeout=10) as resp:
                        image_bytes = resp.read()
                    if _embed_art(fpath, image_bytes):
                        fixed += 1
        except Exception:
            pass
        if progress_cb:
            progress_cb(i + 1, total)
        time.sleep(0.15)  # be polite to Deezer's public API -- see fill_genres.py
    return {"checked": total, "needed_art": total, "fixed": fixed}


def _remove_empty_dirs(root):
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def move_staged_to_library(staging_root, music_dir, existing_keys=None, normalize_key=None, progress_cb=None):
    """Moves every staged file into music_dir/<Artist>/<Artist> -
    <Title>.ext (same collision-avoidance as import_tracks). For each, checks
    whether its (artist, title) -- normalized via `normalize_key`, or a
    simple built-in fallback -- already matches something in
    `existing_keys` (the real library's tracks at the time this is
    called), purely to report it in the returned `duplicates` list.
    Nothing is held back or skipped because of that: this is the point
    where the user finds out about possible duplicates, not where they get
    silently filtered. Also removes the now-empty staging directories once
    everything's moved."""
    normalize_key = normalize_key or _default_normalize_key
    existing_keys = set() if existing_keys is None else existing_keys

    paths = _staged_file_paths(staging_root)
    total = len(paths)
    moved_paths = []
    duplicates = []
    for i, fpath in enumerate(paths):
        info = _read_tags_for_review(fpath) or {"artist": "", "title": os.path.splitext(os.path.basename(fpath))[0]}
        ext = os.path.splitext(fpath)[1].lower()
        dest_folder, natural_path = _natural_staged_path(music_dir, info["artist"], info["title"], ext)
        os.makedirs(dest_folder, exist_ok=True)
        dest_path = organize_by_artist._unique_dest_path(natural_path) if os.path.exists(natural_path) else natural_path
        shutil.move(fpath, dest_path)
        rel = os.path.relpath(dest_path, music_dir)
        moved_paths.append(rel)

        key = normalize_key(info["artist"], info["title"])
        if key in existing_keys:
            duplicates.append({"artist": info["artist"], "title": info["title"], "path": rel})
        existing_keys.add(key)

        if progress_cb:
            progress_cb(i + 1, total)

    # Otherwise this is the one file left behind that keeps staging_root
    # from being empty (see COMPLETE_MARKER above), so it'd never get
    # cleaned up by _remove_empty_dirs below even once every track's moved.
    marker_path = os.path.join(staging_root, COMPLETE_MARKER)
    if os.path.isfile(marker_path):
        os.remove(marker_path)
    _remove_empty_dirs(staging_root)
    return {"total": total, "moved_paths": moved_paths, "duplicates": duplicates}
