"""Audio format conversion via ffmpeg. Always writes a new file -- never
touches or deletes the source. FLAC/ALAC are lossless containers; converting
*into* MP3 320 is a one-way, lossy step, and converting a lossy source
(MP3/AAC) into FLAC/ALAC repackages it without recovering any lost quality --
callers should surface that distinction to the user rather than silently
imply a quality gain.
"""
import os
import re
import shutil
import subprocess
import sys

FORMATS = {
    "flac": {"ext": ".flac", "label": "FLAC", "lossless": True},
    "alac": {"ext": ".m4a", "label": "ALAC", "lossless": True},
    "mp3320": {"ext": ".mp3", "label": "MP3 320", "lossless": False},
}

OUTPUT_ROOT = os.path.join(os.path.expanduser("~"), "Music", "Notorious BPM Converted")

# GUI-launched apps (double-click / Finder / `open`) don't always inherit a
# shell's PATH, so a package manager's install location won't necessarily be
# found via shutil.which alone -- covers Homebrew on macOS and the common
# manual/Scoop install spots on Windows. winget's own ffmpeg installs add
# themselves to PATH directly, so no extra guessing needed there.
if sys.platform == "win32":
    _FFMPEG_NAME = "ffmpeg.exe"
    _CANDIDATE_PATHS = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.join(os.path.expanduser("~"), "scoop", "shims", "ffmpeg.exe"),
    ]
elif sys.platform == "darwin":
    _FFMPEG_NAME = "ffmpeg"
    _CANDIDATE_PATHS = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
else:
    _FFMPEG_NAME = "ffmpeg"
    _CANDIDATE_PATHS = ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]


def _find_ffmpeg():
    found = shutil.which(_FFMPEG_NAME)
    if found:
        return found
    for p in _CANDIDATE_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


FFMPEG_BIN = _find_ffmpeg()


def ffmpeg_available():
    return FFMPEG_BIN is not None


def _safe_component(name):
    name = (name or "Unknown").replace("/", "_").replace(":", "_")
    return re.sub(r"\s{2,}", " ", name).strip() or "Unknown"


def output_path_for(artist, title, fmt, output_root=None):
    info = FORMATS[fmt]
    folder = os.path.join(output_root or OUTPUT_ROOT, info["label"])
    os.makedirs(folder, exist_ok=True)
    fname = _safe_component(f"{artist} - {title}") + info["ext"]
    return os.path.join(folder, fname)


def convert(src_path, artist, title, fmt, output_root=None):
    """Converts src_path to the given format, writing into output_root (the
    user-configured destination -- see /api/convert/output-dir in app.py --
    falling back to OUTPUT_ROOT if not given).
    Returns (output_path, already_existed: bool).
    Raises RuntimeError on ffmpeg failure.
    """
    if not FFMPEG_BIN:
        if sys.platform == "win32":
            hint = "winget install ffmpeg (or choco install ffmpeg / scoop install ffmpeg)"
        elif sys.platform == "darwin":
            hint = "brew install ffmpeg"
        else:
            hint = "your package manager, e.g. apt install ffmpeg"
        raise RuntimeError(f"ffmpeg not found. Install it with: {hint}")
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format: {fmt}")
    if not os.path.isfile(src_path):
        raise RuntimeError(f"Source file not found: {src_path}")

    dest = output_path_for(artist, title, fmt, output_root)
    if os.path.isfile(dest):
        return dest, True

    codec_args = {
        "flac": ["-c:a", "flac"],
        "alac": ["-c:a", "alac"],
        "mp3320": ["-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"],
    }[fmt]
    # Explicit muxer, since the ".part" temp suffix defeats extension-based
    # auto-detection (ffmpeg would otherwise try to infer the container from
    # a filename ending in ".part" and fail).
    muxer = {"flac": "flac", "alac": "ipod", "mp3320": "mp3"}[fmt]

    tmp_dest = dest + ".part"
    cmd = [
        FFMPEG_BIN, "-y", "-i", src_path,
        "-map", "0:a", "-map", "0:v?",
        "-c:v", "copy",
        *codec_args,
        "-map_metadata", "0",
        "-f", muxer,
        tmp_dest,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(tmp_dest):
        # Some inputs choke on "-map 0:v?" (e.g. odd attached-pic streams);
        # retry once without touching video/art at all.
        cmd_fallback = [
            FFMPEG_BIN, "-y", "-i", src_path,
            "-map", "0:a",
            *codec_args,
            "-map_metadata", "0",
            "-f", muxer,
            tmp_dest,
        ]
        result2 = subprocess.run(cmd_fallback, capture_output=True, text=True)
        if result2.returncode != 0 or not os.path.isfile(tmp_dest):
            raise RuntimeError(result2.stderr[-2000:] or result.stderr[-2000:])

    os.rename(tmp_dest, dest)
    return dest, False
