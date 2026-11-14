"""Star ratings stored inside the audio files themselves.

Ratings normally live only in the library file, so a second computer with its
own library never sees them. With "Save ratings inside the music files" on,
each rating is also written as a tag, in the forms other players read:

  MP3        an ID3 POPM frame (1..5 stars -> 1, 64, 128, 196, 255)
  FLAC/OGG   RATING=20..100 (a Vorbis comment)
  M4A        a ----:com.apple.iTunes:RATING freeform atom, 20..100

read() understands the same, plus RATING=1..5 as some players write it.
"""
import os

_POPM_EMAIL = "Notorious B.P.M."
_POPM_BYTE = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
_M4A_KEY = "----:com.apple.iTunes:RATING"


def _stars_from_popm(byte):
    if not byte:
        return 0
    for stars, top in ((1, 31), (2, 95), (3, 159), (4, 223)):
        if byte <= top:
            return stars
    return 5


def _stars_from_number(text):
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    if value <= 5:
        return int(round(value))
    return max(1, min(5, int(round(value / 20.0))))


def read(fpath):
    """0..5; 0 when the file carries no rating (or can't be read)."""
    ext = os.path.splitext(fpath)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3
            frames = ID3(fpath).getall("POPM")
            return max((_stars_from_popm(f.rating) for f in frames), default=0)
        if ext in (".flac", ".ogg"):
            import mutagen
            audio = mutagen.File(fpath)
            values = (audio.tags or {}).get("rating") if audio is not None else None
            return _stars_from_number(values[0]) if values else 0
        if ext == ".m4a":
            from mutagen.mp4 import MP4
            values = (MP4(fpath).tags or {}).get(_M4A_KEY)
            return _stars_from_number(bytes(values[0]).decode("utf-8", "ignore")) if values else 0
    except Exception:
        return 0
    return 0


def write(fpath, stars):
    """Writes (or, for 0, removes) the rating. True if the file was written;
    False for a format that has no rating tag here (WAV) -- errors propagate."""
    stars = max(0, min(5, int(stars or 0)))
    ext = os.path.splitext(fpath)[1].lower()
    if ext == ".mp3":
        from mutagen.id3 import ID3, POPM, ID3NoHeaderError
        try:
            tags = ID3(fpath)
        except ID3NoHeaderError:
            if not stars:
                return True
            tags = ID3()
        tags.delall("POPM")
        if stars:
            tags.add(POPM(email=_POPM_EMAIL, rating=_POPM_BYTE[stars], count=0))
        tags.save(fpath)
        return True
    if ext in (".flac", ".ogg"):
        import mutagen
        audio = mutagen.File(fpath)
        if audio is None:
            return False
        if audio.tags is None:
            audio.add_tags()
        if stars:
            audio.tags["rating"] = [str(stars * 20)]
        elif "rating" in audio.tags:
            del audio.tags["rating"]
        audio.save()
        return True
    if ext == ".m4a":
        from mutagen.mp4 import MP4, MP4FreeForm
        audio = MP4(fpath)
        if audio.tags is None:
            audio.add_tags()
        if stars:
            audio.tags[_M4A_KEY] = [MP4FreeForm(str(stars * 20).encode("utf-8"))]
        elif _M4A_KEY in audio.tags:
            del audio.tags[_M4A_KEY]
        audio.save()
        return True
    return False
