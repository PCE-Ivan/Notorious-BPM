"""The one place audio-file tags are read and written (FLAC, MP3, M4A).

Four modules used to carry their own near-identical per-format writer
(app.py's single-field editor, fill_genres, fill_years, fix_artist_title).
They now share this, which is also what makes every write journalable: a
successful write reports (field, old value, new value) to journal.py, so
any bulk change can be undone.

Fields are the library's own vocabulary -- artist, album, title, genre,
year -- mapped onto each format's real tag names here, once.
"""
import logging

import journal

log = logging.getLogger("jukebox.tagio")

FIELDS = ("artist", "album", "title", "genre", "year")

# EasyID3 and Vorbis comments (FLAC) share these names.
_EASY_KEYS = {"artist": "artist", "album": "album", "title": "title", "genre": "genre",
              "year": "date", "originaldate": "originaldate"}
_M4A_KEYS = {"artist": "\xa9ART", "album": "\xa9alb", "title": "\xa9nam", "genre": "\xa9gen",
             "year": "\xa9day"}


def _open(fpath):
    lower = fpath.lower()
    if lower.endswith(".flac"):
        from mutagen.flac import FLAC
        return "flac", FLAC(fpath)
    if lower.endswith(".mp3"):
        import mutagen
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3NoHeaderError
        try:
            return "mp3", EasyID3(fpath)
        except ID3NoHeaderError:
            audio = mutagen.File(fpath, easy=True)
            if audio is None:
                raise
            audio.add_tags()
            return "mp3", audio
    if lower.endswith(".m4a"):
        from mutagen.mp4 import MP4
        return "m4a", MP4(fpath)
    return None, None


def _key(kind, field):
    return _M4A_KEYS.get(field) if kind == "m4a" else _EASY_KEYS.get(field)


def _first(value):
    if not value:
        return None
    first = value[0]
    return str(first) if first is not None else None


def read_tag(fpath, field):
    """Current value of one field, or None (absent, unreadable, unsupported)."""
    try:
        kind, audio = _open(fpath)
        if audio is None:
            return None
        key = _key(kind, field)
        return _first(audio.get(key)) if key else None
    except Exception:
        return None


def write_tags(fpath, changes, record=True):
    """Set several fields in one save. A value of None/"" removes the tag.
    `changes` is {field: value}; "year" on FLAC/MP3 also sets originaldate
    (as it always has), and that is recorded too so an undo restores it.
    Returns True on success, False on any failure (unsupported format,
    unreadable file, ...). On success each actually-changed field is
    reported to journal.record_tag() unless record=False (which is how an
    undo avoids journaling itself)."""
    try:
        kind, audio = _open(fpath)
        if audio is None:
            return False
        changed = []
        pending = dict(changes)
        if kind in ("flac", "mp3") and "year" in pending and "originaldate" not in pending:
            pending["originaldate"] = pending["year"]
        for field, value in pending.items():
            key = _key(kind, field)
            if not key:
                continue
            old = _first(audio.get(key))
            new = None if value is None or str(value) == "" else str(value)
            if new is None:
                if key in audio:
                    del audio[key]
            else:
                audio[key] = [new]
            if old != new:
                changed.append((field, old, new))
        audio.save()
    except Exception:
        log.exception("Could not write tags to %s", fpath)
        return False
    if record:
        for field, old, new in changed:
            journal.record_tag(fpath, field, old, new)
    return True


def write_tag(fpath, field, value, record=True):
    return write_tags(fpath, {field: value}, record=record)
