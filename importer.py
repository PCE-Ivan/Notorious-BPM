"""Adds music from outside the library: files or folders dropped on the window,
or picked from the Library menu.

Copies -- never moves -- so the originals are untouched, into
<music folder>/<Artist>/ (the same layout "Organize by Artist" produces).
A track whose artist + title is already in the library is skipped and
reported, with both copies' apparent bitrates so you can see whether the
dropped one is actually better; the library's own duplicate tools take it
from there. Files already inside the music folder aren't copied at all (a
rescan picks them up).
"""
import os
import shutil

import organize_by_artist
import scan_library


def collect_audio_files(paths):
    """Every audio file under the given files/folders, de-duplicated, in a
    stable order. Skips AppleDouble sidecars and hidden folders."""
    found, seen = [], set()

    def add(path):
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            found.append(path)

    for p in paths:
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    if name.lower().endswith(scan_library.EXTS) and scan_library._is_real_audio_file(name):
                        add(os.path.join(root, name))
        elif os.path.isfile(p):
            name = os.path.basename(p)
            if name.lower().endswith(scan_library.EXTS) and scan_library._is_real_audio_file(name):
                add(p)
    return found


def read_identity(path):
    """(artist, title, duration_seconds); any may be None."""
    import mutagen
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None, None, None
    if audio is None:
        return None, None, None

    def first(key):
        vals = audio.tags.get(key) if audio.tags else None
        return str(vals[0]).strip() or None if vals else None

    length = getattr(audio.info, "length", None) if audio.info else None
    return first("artist"), first("title"), length


def _bitrate(path, duration):
    if not duration:
        return None
    try:
        return round(os.path.getsize(path) * 8 / duration / 1000)
    except OSError:
        return None


def _inside(path, folder):
    real, root = os.path.realpath(path), os.path.realpath(folder)
    return real == root or real.startswith(root + os.sep)


def import_files(paths, music_dir, existing_index, normalize_key, progress_cb=None):
    """Copies what isn't already there. `existing_index` maps normalize_key(
    artist, title) -> {"path": relpath, "duration": s} for the library as it
    stands; copies made during this run are added to it, so the same song
    dropped twice is only copied once.

    Returns {"total", "copied": [relpath...], "duplicates": [...],
    "in_library": n, "errors": [{"file", "error"}...]}."""
    files = collect_audio_files(paths)
    total = len(files)
    result = {"total": total, "copied": [], "duplicates": [], "in_library": 0, "errors": []}
    index = dict(existing_index)

    for i, src in enumerate(files):
        if progress_cb:
            progress_cb(i, total)
        if _inside(src, music_dir):
            result["in_library"] += 1
            continue
        try:
            artist, title, duration = read_identity(src)
            key = normalize_key(artist, title) if title else None
            hit = index.get(key) if key else None
            if hit is not None:
                result["duplicates"].append({
                    "artist": artist, "title": title, "library_path": hit["path"],
                    "dropped_bitrate": _bitrate(src, duration),
                    "library_bitrate": _bitrate(os.path.join(music_dir, hit["path"]), hit.get("duration")),
                })
                continue
            folder = os.path.join(music_dir, organize_by_artist._sanitize_artist_name(artist or "Unknown Artist"))
            os.makedirs(folder, exist_ok=True)
            natural = os.path.join(folder, os.path.basename(src))
            try:
                if os.path.getsize(natural) == os.path.getsize(src):
                    result["in_library"] += 1   # the same file dropped again (or imported before)
                    continue
            except OSError:
                pass
            dest = organize_by_artist._unique_dest_path(natural)
            part = dest + ".part"   # not an audio extension, so a scan never sees a half-written file
            try:
                shutil.copy2(src, part)
                os.replace(part, dest)
            finally:
                if os.path.exists(part):
                    os.remove(part)
            rel = os.path.relpath(dest, music_dir)
            result["copied"].append(rel)
            if key:
                index[key] = {"path": rel, "duration": duration}
        except OSError as e:
            result["errors"].append({"file": os.path.basename(src), "error": e.strerror or str(e)})
    if progress_cb:
        progress_cb(total, total)
    return result
