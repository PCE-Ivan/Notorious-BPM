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


def import_tracks(mount, music_dir, existing_keys=None, normalize_key=None, progress_cb=None):
    """Copies every track this can resolve into music_dir, laid out exactly
    like the rest of the library: music_dir/<Artist>/<Artist> - <Title>.ext.
    A track whose (artist, title) -- normalized via `normalize_key`, or a
    simple built-in fallback -- already matches something in `existing_keys`
    is skipped outright, on the assumption it's already in the library under
    some other file. A destination file that already exists at exactly the
    source's size is treated as "already imported in a previous run" and
    left alone rather than re-copied or renamed to a "(2)" sibling; anything
    else occupying that exact name gets the same "(2)", "(3)", ... suffix
    organize_by_artist.py itself would use. `existing_keys` is mutated as
    tracks are accounted for, so a second song on the iPod matching one
    already handled earlier in this same run (including two copies of the
    same song on the iPod itself) is skipped too, not just library repeats.
    Source files are only ever read, never modified or deleted."""
    normalize_key = normalize_key or _default_normalize_key
    existing_keys = set() if existing_keys is None else existing_keys

    itunesdb_path = os.path.join(mount, "iPod_Control", "iTunes", "iTunesDB")
    tracks = parse_itunesdb(itunesdb_path, mount)
    os.makedirs(music_dir, exist_ok=True)

    copied_paths = []
    already_imported = 0
    duplicate_skipped = 0
    total = len(tracks)
    for i, t in enumerate(tracks):
        key = normalize_key(t["artist"], t["title"])
        if key in existing_keys:
            duplicate_skipped += 1
        else:
            ext = os.path.splitext(t["real_path"])[1].lower()
            artist_label = _safe_component(t["artist"], "Unknown Artist")
            title_label = _safe_component(t["title"], "Untitled")
            dest_folder = os.path.join(music_dir, artist_label)
            natural_path = os.path.join(dest_folder, f"{artist_label} - {title_label}{ext}")

            src_size = os.path.getsize(t["real_path"])
            if os.path.isfile(natural_path) and os.path.getsize(natural_path) == src_size:
                already_imported += 1
            else:
                os.makedirs(dest_folder, exist_ok=True)
                dest_path = (
                    organize_by_artist._unique_dest_path(natural_path)
                    if os.path.exists(natural_path) else natural_path
                )
                shutil.copy2(t["real_path"], dest_path)
                copied_paths.append(os.path.relpath(dest_path, music_dir))
            existing_keys.add(key)

        if progress_cb:
            progress_cb(i + 1, total)

    return {
        "total": total,
        "copied": len(copied_paths),
        "already_imported": already_imported,
        "duplicate_skipped": duplicate_skipped,
        "copied_paths": copied_paths,
    }
