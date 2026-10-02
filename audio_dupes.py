"""Finds the same recording in a library by how it *sounds*.

The Duplicates tool matches on artist + title, which misses copies whose tags
differ ("Artist - Song.mp3" vs a track tagged "Song (feat. X)") and trusts
copies whose tags agree but whose audio doesn't. This compares Chromaprint
fingerprints instead -- the same fingerprints AcoustID uses, but compared
locally, so it needs no network and no API key, just the `fpcalc` binary the
app already looks for.

Pipeline (see app.py for the background job around it):

  1. compute_fingerprint() -- `fpcalc -raw`: one 32-bit value per ~0.124 s of
     audio (the first 2 minutes is plenty), cached in the library file, so a
     track is only ever decoded once.
  2. find_groups() -- bucket tracks by duration (a different length is a
     different edit), then within a bucket look up each fingerprint value in
     an inverted index. Two copies of one recording share many values at one
     consistent time offset, so the votes pick the offset (it can be several
     seconds, e.g. different lead-in silence) without trying them all.
  3. The best offset is then verified with the bit error rate over the
     overlap -- the fraction of differing bits. Different encodings of the
     same audio land around 0.05-0.20; unrelated music sits near 0.5.

Pure Python on purpose: the packaged app has no numpy.
"""
import array
import json
import subprocess
import sys
from collections import Counter, defaultdict

FRAME_SECONDS = 0.1238          # Chromaprint: 4096-sample frames, 2/3 overlap, at 11.025 kHz
FINGERPRINT_SECONDS = 120       # how much of each track is fingerprinted
ALGORITHM = 1                   # bump to invalidate every cached fingerprint

DURATION_TOLERANCE = 5.0        # seconds; a longer/shorter edit is a different recording
MIN_OVERLAP = 0.6               # of the shorter fingerprint, after alignment
MAX_BER = 0.25                  # at or below this, same recording
INDEX_MASK = 0xFFFFFF00         # index on the upper 24 bits: the low bits are the noisiest
MIN_VOTES = 4                   # agreeing index hits before an offset is worth verifying
MAX_POSTINGS = 40               # values this common (silence, a drone) say nothing

_POP16 = bytes(bin(i).count("1") for i in range(1 << 16))
assert array.array("I").itemsize == 4


NO_AUDIO = "no audio to fingerprint"


class FingerprintError(Exception):
    pass


# ------------------------------------------------------------ fingerprinting --
def compute_fingerprint(fpcalc_path, path, seconds=FINGERPRINT_SECONDS, timeout=90):
    """(duration_seconds, array('I')) for an audio file."""
    try:
        proc = subprocess.run(
            [fpcalc_path, "-raw", "-json", "-length", str(seconds), path],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise FingerprintError("fpcalc timed out")
    if proc.returncode != 0:
        lines = (proc.stderr or "").strip().splitlines()
        if "empty fingerprint" in (proc.stderr or "").lower():
            raise FingerprintError(NO_AUDIO)  # silence, or too short -- retrying won't change that
        raise FingerprintError(lines[-1] if lines else "fpcalc failed")
    try:
        data = json.loads(proc.stdout)
        raw = data["fingerprint"]
        duration = float(data.get("duration") or 0)
    except (ValueError, KeyError, TypeError):
        raise FingerprintError("unreadable fpcalc output")
    if isinstance(raw, str):
        raw = [int(x) for x in raw.split(",") if x.strip()]
    if not raw:
        raise FingerprintError(NO_AUDIO)
    return duration, pack(raw)


def pack(values):
    return array.array("I", (int(v) & 0xFFFFFFFF for v in values))


def to_blob(fp):
    out = array.array("I", fp)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


def from_blob(blob):
    out = array.array("I")
    out.frombytes(bytes(blob))
    if sys.byteorder == "big":
        out.byteswap()
    return out


# ---------------------------------------------------------------------- cache --
def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_fingerprints (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
            fp BLOB NOT NULL,
            duration REAL,
            algo INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    # Results of the AcoustID "does this audio match its tags?" check, so a
    # whole-library run can stop and resume (or survive a restart) and a
    # dismissed mismatch stays dismissed.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_verified (
            track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
            checked_at REAL NOT NULL,
            status TEXT NOT NULL,
            found_artist TEXT,
            found_title TEXT,
            score REAL,
            dismissed INTEGER NOT NULL DEFAULT 0
        )
    """)
    # "These aren't duplicates" answers, so a reviewed group never comes
    # back. Keyed by the sorted track ids, not by title, so it survives tag
    # edits but not a deleted/replaced file.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dup_dismissed (
            key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)


def group_key(track_ids):
    return ",".join(str(i) for i in sorted(int(i) for i in track_ids))


def tracks_needing_fingerprint(conn):
    """Rows (id, path, duration) with no cached fingerprint for this algorithm.
    A cached one stays valid across tag edits -- they don't change the audio --
    and is dropped with its track (ON DELETE CASCADE)."""
    return conn.execute(
        "SELECT t.id, t.path, t.duration FROM tracks t "
        "LEFT JOIN audio_fingerprints f ON f.track_id = t.id AND f.algo = ? "
        "WHERE f.track_id IS NULL ORDER BY t.id", (ALGORITHM,)
    ).fetchall()


def load_fingerprints(conn):
    """[(track_id, duration, array('I'))] for every cached fingerprint."""
    out = []
    for row in conn.execute("SELECT track_id, duration, fp FROM audio_fingerprints WHERE algo = ?", (ALGORITHM,)):
        out.append((row[0], row[1] or 0.0, from_blob(row[2])))
    return out


# ------------------------------------------------------------------ matching --
def ber_at(a, b, offset):
    """(bit error rate, overlap in frames) comparing a[i] with b[i + offset]."""
    lo = max(0, -offset)
    hi = min(len(a), len(b) - offset)
    n = hi - lo
    if n <= 0:
        return 1.0, 0
    pop = _POP16
    bits = 0
    for i in range(lo, hi):
        x = a[i] ^ b[i + offset]
        bits += pop[x & 0xFFFF] + pop[x >> 16]
    return bits / (32.0 * n), n


def compare(a, b, offset_hint=None):
    """Best (ber, offset) for two fingerprints, trying the hinted offsets
    (and their neighbours: the vote is noisy by a frame). None if they don't
    overlap enough to judge."""
    need = MIN_OVERLAP * min(len(a), len(b))
    best = None
    for off in offset_hint or ():
        ber, n = ber_at(a, b, off)
        if n >= need and (best is None or ber < best[0]):
            best = (ber, off)
    return best


def _candidate_offsets(votes):
    """Offsets worth verifying from a Counter of offset -> votes: the
    strongest, with a vote's +/-1 neighbours folded in (frame-boundary jitter
    splits one true offset across adjacent counts)."""
    if not votes:
        return []
    scored = []
    for off, v in votes.items():
        total = v + votes.get(off - 1, 0) + votes.get(off + 1, 0)
        scored.append((total, off))
    scored.sort(reverse=True)
    if scored[0][0] < MIN_VOTES:
        return []
    top = scored[0][0]
    picked = []
    for total, off in scored[:6]:
        if total >= max(MIN_VOTES, top * 0.5):
            picked.extend((off - 1, off, off + 1))
    return list(dict.fromkeys(picked))


def find_pairs(entries, progress=None, duration_tolerance=DURATION_TOLERANCE, max_ber=MAX_BER):
    """Every pair of entries that are the same recording, as
    [(id_a, id_b, ber), ...]. `entries` is [(track_id, duration, fp), ...];
    `progress(done, total)` is called as buckets are worked through (and may
    raise to cancel)."""
    width = max(1.0, duration_tolerance)
    buckets = defaultdict(list)
    for track_id, duration, fp in entries:
        if len(fp) >= 8:
            buckets[int(duration // width)].append((track_id, duration, fp))
    keys = sorted(buckets)
    total = sum(len(buckets[k]) for k in keys)
    done = 0
    pairs = []
    seen = set()

    for k in keys:
        primary = buckets[k]
        group = primary + buckets.get(k + 1, [])
        n_primary = len(primary)
        if len(group) < 2:
            done += n_primary
            if progress:
                progress(done, total)
            continue

        postings = defaultdict(list)
        for gi, (_tid, _dur, fp) in enumerate(group):
            for pos in range(0, len(fp), 2):
                postings[fp[pos] & INDEX_MASK].append((gi, pos))
        for v in [v for v, lst in postings.items() if len(lst) > MAX_POSTINGS]:
            del postings[v]

        for gi in range(len(group)):
            tid_b, dur_b, fp_b = group[gi]
            votes_by_other = defaultdict(Counter)
            for j in range(len(fp_b)):
                for gj, pos in postings.get(fp_b[j] & INDEX_MASK, ()):
                    if gj >= gi:
                        continue
                    # a[pos] ~ b[j]  =>  b is shifted by (j - pos) relative to a
                    votes_by_other[gj][j - pos] += 1
            for gj, votes in votes_by_other.items():
                if gi >= n_primary and gj >= n_primary:
                    continue  # both in the next bucket: that bucket's own pass covers it
                tid_a, dur_a, fp_a = group[gj]
                if abs(dur_a - dur_b) > duration_tolerance:
                    continue
                key = (tid_a, tid_b) if tid_a < tid_b else (tid_b, tid_a)
                if key in seen:
                    continue
                offsets = _candidate_offsets(votes)
                if not offsets:
                    continue
                # compare() aligns a[i] with b[i + off]; the votes are in b-minus-a terms.
                best = compare(fp_a, fp_b, offsets)
                seen.add(key)
                if best and best[0] <= max_ber:
                    pairs.append((key[0], key[1], round(best[0], 4)))
            if gi < n_primary:
                done += 1
                if progress and done % 20 == 0:
                    progress(done, total)
        if progress:
            progress(done, total)
    return pairs


def group_pairs(pairs):
    """Union the pairs into groups: [{"ids": [...], "ber": worst pair}, ...],
    largest first. A chain A~B, B~C groups all three; "ber" is the weakest
    link so the review screen can say how sure it is."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ber in pairs:
        parent[find(a)] = find(b)
    members = defaultdict(set)
    for a, b, _ber in pairs:
        members[find(a)].update((a, b))
    worst = defaultdict(float)
    for a, b, ber in pairs:
        root = find(a)
        worst[root] = max(worst[root], ber)
    groups = [{"ids": sorted(ids), "ber": worst[root]} for root, ids in members.items()]
    groups.sort(key=lambda g: (-len(g["ids"]), g["ber"], g["ids"][0]))
    return groups
