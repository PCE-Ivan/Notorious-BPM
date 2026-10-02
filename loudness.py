"""Per-track loudness, for "level volume across tracks" (ReplayGain-style).

Shuffle across a library jumps between a 1985 ballad and a 2019 single that is
10 dB louder, and the volume knob is the only fix. This measures each track's
integrated loudness (EBU R128, via ffmpeg's ebur128 filter) once and caches it
in the library file; the player then turns loud tracks down toward a common
target. Only ever *down*: the player keeps Web Audio off the real <audio>
element (AirPlay depends on that), and element volume can't go above 1 -- so
quiet tracks play at the volume you set, and loud ones come down to meet them.
"""
import re
import subprocess

TARGET_LUFS = -14.0          # what loud tracks are brought down to
MEASURE_SECONDS = 90         # the first minute and a half is representative, and ~2x cheaper than two-and-a-half

_I_RE = re.compile(r"^\s*I:\s+(-?\d+(?:\.\d+)?|-inf)\s+LUFS", re.MULTILINE)
_PEAK_RE = re.compile(r"^\s*Peak:\s+(-?\d+(?:\.\d+)?|-inf)\s+dBFS", re.MULTILINE)


class LoudnessError(Exception):
    pass


def parse_ebur128(stderr):
    """(lufs, peak_dbfs) from the *last* summary block ffmpeg prints (the
    per-frame lines earlier in the output also contain "I:")."""
    summary = stderr.rsplit("Summary:", 1)
    if len(summary) < 2:
        raise LoudnessError("no loudness summary in ffmpeg output")
    block = summary[1]
    m = _I_RE.search(block)
    if not m or m.group(1) == "-inf":
        raise LoudnessError("silent or unmeasurable audio")
    peak = None
    pm = _PEAK_RE.search(block)
    if pm and pm.group(1) != "-inf":
        peak = float(pm.group(1))
    return float(m.group(1)), peak


def measure(ffmpeg_path, path, seconds=MEASURE_SECONDS, timeout=180):
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-nostats", "-nostdin", "-t", str(seconds), "-i", path,
             "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise LoudnessError("ffmpeg timed out")
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        raise LoudnessError(lines[-1] if lines else "ffmpeg failed")
    return parse_ebur128(proc.stderr)


def gain_db(lufs, peak=None, target=TARGET_LUFS):
    """dB to apply (always <= 0): loud tracks come down to the target, quiet
    ones are left alone."""
    if lufs is None:
        return None
    return round(min(0.0, target - lufs), 2)


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_loudness (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
            lufs REAL,
            peak REAL,
            checked_at REAL NOT NULL
        )
    """)


def tracks_needing_measurement(conn):
    return conn.execute(
        "SELECT t.id, t.path FROM tracks t LEFT JOIN audio_loudness l ON l.track_id = t.id "
        "WHERE l.track_id IS NULL ORDER BY t.id").fetchall()
