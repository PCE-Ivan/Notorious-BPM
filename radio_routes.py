"""Live internet radio: the station directory, the stream proxy (for VU
meters and "now playing"), song identification, and the level meter feed.

Moved out of app.py as its own blueprint -- nothing here touches the music
library's files or its globals; the one thing it needs from the app is a
database connection (for "Match My Library"), handed over as `get_db` when
app.py registers the blueprint.
"""
import cmath
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from flask import Blueprint, Response, abort, jsonify, request

import config as jukebox_config
import local_guard

bp = Blueprint("radio", __name__)
get_db = None  # set by app.py: the request-scoped library connection


# ------------------------------------------------------------ internet radio --
# Backed by Radio Browser (radio-browser.info), a free, open directory of
# public station streams and their metadata (tags, country, popularity
# votes/clicks) -- this app never hosts or caches any station's audio, it
# only looks up and plays each station's own publicly broadcast stream URL,
# same as tuning a physical radio or a browser hitting the URL directly, so
# there's no copyright exposure on our side; each station is responsible for
# its own licensing.
RADIO_UA = "NotoriousBPM/1.0"

# GUI-launched apps on macOS (double-clicked from Finder/Dock, or opened via
# `open`) don't reliably inherit the same PATH a Terminal shell has --
# Homebrew's install location is a common casualty of this, so a tool that's
# genuinely installed and on PATH in Terminal can still come up "missing"
# here via a bare shutil.which(). Checking these well-known install
# locations directly as a fallback covers that without requiring the user
# to do anything about their shell environment.
_COMMON_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin")


def _find_binary(name):
    found = shutil.which(name)
    if found:
        return found
    for d in _COMMON_BIN_DIRS:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


RADIO_FALLBACK_HOSTS = (
    "de1.api.radio-browser.info",
    "de2.api.radio-browser.info",
    "nl1.api.radio-browser.info",
    "at1.api.radio-browser.info",
)

# Radio Browser's raw tag list runs to tens of thousands of mostly noisy,
# user-submitted values (duplicates, typos, non-genre tags) -- a curated
# list makes for a usable dropdown; each value is still just passed straight
# through as their `tag` search param, so it's not a hardcoded ID of
# anything on their end that could later disappear.
RADIO_GENRES = (
    "pop", "rock", "jazz", "classical", "electronic", "hip hop", "dance",
    "country", "reggae", "metal", "blues", "folk", "indie", "house",
    "techno", "ambient", "oldies", "latin", "disco", "punk", "soul", "funk",
    "world", "news", "talk", "sports", "k-pop", "chillout", "lounge", "gospel",
)

_radio_cache = {"countries": None, "countries_ts": 0}
RADIO_CACHE_TTL = 24 * 3600


def _radio_hosts():
    """Live mirror hostnames via all.api.radio-browser.info's round-robin
    DNS, shuffled so repeated calls spread across mirrors instead of
    hammering whichever sorts first -- falls back to a small static list if
    DNS resolution itself fails (offline, or that domain is unreachable)."""
    try:
        _, _, ips = socket.gethostbyname_ex("all.api.radio-browser.info")
        hosts, seen = [], set()
        for ip in ips:
            try:
                host = socket.gethostbyaddr(ip)[0]
            except socket.herror:
                continue
            if host not in seen:
                seen.add(host)
                hosts.append(host)
        random.shuffle(hosts)
        return hosts or list(RADIO_FALLBACK_HOSTS)
    except socket.gaierror:
        return list(RADIO_FALLBACK_HOSTS)


def _radio_get(path, params=None, timeout=8):
    """Tries live mirrors in turn until one answers -- individual Radio
    Browser mirrors go down or rotate over time, so hardcoding a single one
    would eventually just break."""
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    last_err = None
    for host in _radio_hosts()[:5]:
        url = f"https://{host}{path}{query}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": RADIO_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Couldn't reach the radio directory ({last_err}).")


@bp.route("/api/radio/genres")
def radio_genres():
    return jsonify(list(RADIO_GENRES))


@bp.route("/api/radio/countries")
def radio_countries():
    now = time.time()
    if _radio_cache["countries"] is None or now - _radio_cache["countries_ts"] > RADIO_CACHE_TTL:
        try:
            raw = _radio_get("/json/countries", {"order": "name"})
            # Radio Browser's raw list carries the same country twice under
            # differently-cased ISO codes for a handful of entries (e.g. "US"
            # and "us") -- collapse by uppercased code, keeping whichever
            # duplicate has more stations, so the dropdown doesn't show the
            # same country name side by side twice.
            by_code = {}
            for c in raw:
                code = (c.get("iso_3166_1") or "").upper()
                if not code or not c.get("name") or not c.get("stationcount", 0) > 0:
                    continue
                if code not in by_code or c["stationcount"] > by_code[code]["count"]:
                    by_code[code] = {"code": code, "name": c["name"], "count": c["stationcount"]}
            countries = sorted(by_code.values(), key=lambda c: c["name"])
            _radio_cache["countries"] = countries
            _radio_cache["countries_ts"] = now
        except RuntimeError:
            if _radio_cache["countries"] is None:
                return jsonify({"ok": False, "error": "Couldn't reach the radio directory.", "countries": []}), 502
    return jsonify({"ok": True, "countries": _radio_cache["countries"]})


def _format_stations(raw):
    return [
        {
            "uuid": s.get("stationuuid"),
            "name": s.get("name") or "Unnamed station",
            "url": s.get("url_resolved") or s.get("url"),
            "favicon": s.get("favicon") or None,
            "country": s.get("country") or None,
            "tags": s.get("tags") or None,
            "bitrate": s.get("bitrate") or None,
            "votes": s.get("votes"),
            "clickcount": s.get("clickcount"),
        }
        for s in raw
        if s.get("url_resolved") or s.get("url")
    ]


@bp.route("/api/radio/stations")
def radio_stations():
    genre = (request.args.get("genre") or "").strip()
    country_code = (request.args.get("country") or "").strip()
    sort = request.args.get("sort", "votes")
    try:
        limit = min(max(int(request.args.get("limit", 60)), 1), 200)
    except ValueError:
        limit = 60

    params = {
        "order": sort if sort in ("votes", "clickcount", "bitrate", "name") else "votes",
        "reverse": "true",
        "limit": limit,
        "hidebroken": "true",
    }
    if genre:
        params["tag"] = genre
    if country_code:
        params["countrycode"] = country_code

    try:
        raw = _radio_get("/json/stations/search", params)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e), "stations": []}), 502

    return jsonify({"ok": True, "stations": _format_stations(raw)})


# A library genre can be free-text and often compound ("Rap/Hip Hop",
# "Chill Out/Trip-Hop/Lounge") -- this maps it to the closest tag in our own
# curated RADIO_GENRES list rather than passing it straight through to
# Radio Browser's tag search, where the exact library string would usually
# match nothing at all.
_GENRE_MATCH_ALIASES = {
    "rap": "hip hop", "r&b": "soul", "rnb": "soul", "trip-hop": "chillout",
    "trip hop": "chillout", "edm": "electronic", "alternative": "indie",
    "singer-songwriter": "folk", "orchestral": "classical", "afrobeat": "world",
    "african": "world", "asian": "world", "worldbeat": "world",
}


def _match_radio_genre(library_genre):
    norm = library_genre.lower()
    for tag in RADIO_GENRES:
        if tag in norm or norm in tag:
            return tag
    for key, tag in _GENRE_MATCH_ALIASES.items():
        if key in norm:
            return tag
    return None


@bp.route("/api/radio/match")
def radio_match():
    """"Because your library leans on X, here's live radio playing similar
    music" -- takes the library's own most-common genres (by track count),
    maps the first one with a usable match to one of our curated radio tags,
    and searches Radio Browser with it. Falls through to the next
    most-common library genre if one has no reasonable radio-tag match."""
    db = get_db()
    rows = db.execute(
        "SELECT primary_genre, COUNT(*) as cnt FROM tracks "
        "WHERE primary_genre IS NOT NULL AND primary_genre != '' "
        "GROUP BY primary_genre ORDER BY cnt DESC LIMIT 15"
    ).fetchall()

    matched_tag = None
    matched_genre = None
    for row in rows:
        tag = _match_radio_genre(row["primary_genre"])
        if tag:
            matched_tag = tag
            matched_genre = row["primary_genre"]
            break

    if not matched_tag:
        return jsonify({"ok": False, "error": "no_match"})

    params = {"order": "votes", "reverse": "true", "limit": 30, "hidebroken": "true", "tag": matched_tag}
    try:
        raw = _radio_get("/json/stations/search", params)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    return jsonify({
        "ok": True, "library_genre": matched_genre, "matched_tag": matched_tag,
        "stations": _format_stations(raw),
    })


@bp.route("/api/radio/click/<uuid>", methods=["POST"])
def radio_click(uuid):
    # Purely a popularity signal back to Radio Browser (their vote/click
    # counters, used for the "sort by popularity" option) -- never lets a
    # failure here interrupt playback, which has already started against
    # the URL the frontend already has.
    try:
        _radio_get(f"/json/url/{uuid}")
    except Exception:
        pass
    return jsonify({"ok": True})


# Latest parsed "Artist - Title" (from ICY metadata, see radio_proxy below)
# for each in-flight proxied stream, keyed by a random id the frontend mints
# per play -- lets /api/radio/now-playing answer "what's on air right now"
# without the frontend needing any access to the raw byte stream itself.
_radio_now_playing = {}
_radio_now_playing_lock = threading.Lock()
_ICY_TITLE_RE = re.compile(r"StreamTitle='([^']*)'")


@bp.route("/api/radio/proxy")
def radio_proxy():
    """Proxies a station's live stream through this server instead of
    letting the browser hit it directly. Two independent reasons this
    exists, both requiring the same fix:

    1. Web Audio's AnalyserNode (what drives the Hi-Fi theme's VU meters)
       silently returns all-zero data for cross-origin audio unless the
       remote server sends CORS headers -- virtually no Icecast/Shoutcast
       radio stream does. Proxied through here, the browser sees a
       same-origin URL, so the analyser gets real samples. Playback itself
       never needed this (a plain <audio src> to the external URL plays
       fine); only the visualizer tap does.
    2. The actual "now playing" artist/track for a station is carried as
       ICY metadata interleaved *inside* the audio byte stream itself
       (a `StreamTitle='...'` block every `icy-metaint` bytes) -- there's no
       way for client-side JS to see that at all from a direct <audio src>,
       since browsers parse and discard it silently. Reading the stream
       here lets us pull each StreamTitle out, strip it back out of the
       bytes actually sent to the browser (leaving pure audio, since
       feeding the raw interleaved bytes to <audio> would corrupt playback),
       and hand the parsed title to /api/radio/now-playing to poll.
    """
    url = request.args.get("url", "")
    sid = request.args.get("sid", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        abort(400)
    if local_guard.refuse_private_url(url):
        abort(400)  # not a way to reach the router or other things on the local network

    req = urllib.request.Request(url, headers={"User-Agent": RADIO_UA, "Icy-MetaData": "1"})
    try:
        upstream = urllib.request.urlopen(req, timeout=10)
    except Exception:
        abort(502)

    content_type = upstream.headers.get("Content-Type", "audio/mpeg")
    try:
        metaint = int(upstream.headers.get("icy-metaint", 0))
    except ValueError:
        metaint = 0

    def generate():
        try:
            if metaint <= 0:
                # Station doesn't support ICY metadata -- still worth
                # proxying for the CORS/VU-meter fix, just with no title.
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    yield chunk
                return

            while True:
                audio_chunk = upstream.read(metaint)
                if not audio_chunk:
                    break
                yield audio_chunk

                len_byte = upstream.read(1)
                if not len_byte:
                    break
                meta_len = len_byte[0] * 16
                if meta_len:
                    meta = upstream.read(meta_len).rstrip(b"\x00").decode("utf-8", "ignore")
                    m = _ICY_TITLE_RE.search(meta)
                    if m and sid:
                        with _radio_now_playing_lock:
                            _radio_now_playing[sid] = m.group(1).strip()
        finally:
            upstream.close()
            if sid:
                with _radio_now_playing_lock:
                    _radio_now_playing.pop(sid, None)

    return Response(generate(), mimetype=content_type)


@bp.route("/api/radio/now-playing")
def radio_now_playing():
    sid = request.args.get("sid", "")
    with _radio_now_playing_lock:
        title = _radio_now_playing.get(sid)
    return jsonify({"title": title})


@bp.route("/api/radio/acoustid-key")
def get_acoustid_key():
    return jsonify({"acoustidApiKey": jukebox_config.load_config().get("acoustidApiKey") or ""})


@bp.route("/api/radio/acoustid-key", methods=["POST"])
def set_acoustid_key():
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("acoustidApiKey") or "").strip()
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("acoustidApiKey", key))
    return jsonify({"ok": True})


def _fingerprint_lookup(fpath, api_key, fpcalc_path, timeout=30):
    """Runs Chromaprint (fpcalc) against a local audio file and looks the
    fingerprint up on AcoustID -- the free, open, MusicBrainz-backed
    fingerprint database tools like Picard use. Shared by radio_identify
    (fingerprints a live-captured clip) and the library audio-verification
    feature below (fingerprints a file already on disk) -- the "read
    fpcalc's json, call AcoustID, pick the best-scoring match" logic is
    identical either way; only how the audio bytes get there differs.
    Returns (artist, title, score) for the highest-confidence match with
    both a title and at least one credited artist, or (None, None, None)
    if fpcalc or the lookup itself found nothing usable. Raises on a
    genuine I/O/network error -- callers decide how to report that."""
    result = subprocess.run([fpcalc_path, "-json", fpath], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 or not result.stdout:
        return None, None, None
    fp_data = json.loads(result.stdout)

    params = {
        "client": api_key,
        "fingerprint": fp_data["fingerprint"],
        "duration": int(fp_data["duration"]),
        "meta": "recordings",
    }
    lookup_url = "https://api.acoustid.org/v2/lookup?" + urllib.parse.urlencode(params)
    lookup_req = urllib.request.Request(lookup_url, headers={"User-Agent": RADIO_UA})
    with urllib.request.urlopen(lookup_req, timeout=15) as resp:
        lookup = json.loads(resp.read().decode("utf-8"))

    if lookup.get("status") != "ok":
        msg = (lookup.get("error") or {}).get("message", "Lookup failed")
        raise RuntimeError(msg)

    results = lookup.get("results", [])
    if not results:
        return None, None, None
    best_score = max(r.get("score", 0) for r in results)

    # One AcoustID "result" (one fingerprint match, one score) is routinely
    # linked to several different MusicBrainz recordings -- other users'
    # separate submissions of the same actual song, not a confidence
    # ranking of its own. Taking whichever recording happened to be listed
    # first isn't picking the best match, it's picking an arbitrary
    # submission order: this returned "Man Made" for a real A Flock of
    # Seagulls file that is unambiguously "Wishing (...)" -- 5 of the 6
    # recordings tied to that one 0.987-confidence result agreed it was
    # "Wishing", only 1 said "Man Made", and the old first-wins logic
    # happened to hit that lone wrong one first. Majority vote instead,
    # among only the recordings tied to the top score.
    def norm(s):
        return re.sub(r"[^\w]", "", (s or "").lower())

    votes = {}  # (norm(artist), norm(title)) -> [count, artist, title]
    for r in results:
        if r.get("score", 0) != best_score:
            continue
        for rec in r.get("recordings", []):
            title, artists = rec.get("title"), rec.get("artists")
            if not title or not artists:
                continue
            artist = ", ".join(a["name"] for a in artists if a.get("name"))
            key = (norm(artist), norm(title))
            entry = votes.setdefault(key, [0, artist, title])
            entry[0] += 1

    if not votes:
        return None, None, None
    _count, artist, title = max(votes.values(), key=lambda v: v[0])
    return artist, title, best_score


@bp.route("/api/radio/identify", methods=["POST"])
def radio_identify():
    """Shazam-style "what's this song" for a station whose ICY metadata is
    missing, empty, or just repeats the station's own name/slogan.
    Fingerprints ~15s of live audio and looks it up via _fingerprint_lookup.
    No per-query cost, nothing stored; the captured clip is a temp file
    deleted right after."""
    # These "soft" failures all return HTTP 200 -- the frontend needs to
    # tell them apart (missing key vs missing binary vs no match) to show
    # the right message/prompt, and api()'s fetch wrapper throws away the
    # JSON body entirely for any non-2xx response, same convention already
    # used by /api/choose-folder's {"ok": false, "cancelled": true}.
    fpcalc_path = _find_binary("fpcalc")
    if not fpcalc_path:
        return jsonify({"ok": False, "error": "fpcalc_missing"})

    api_key = (jukebox_config.load_config().get("acoustidApiKey") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "no_api_key"})

    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        abort(400)

    tmp_path = None
    try:
        # Plain capture, no Icy-MetaData request -- fpcalc needs a clean,
        # uninterrupted clip; the ICY "now playing" title (if any) is
        # handled entirely separately by the live proxy above. Bounded by
        # wall-clock time (not byte count) so a ~15s clip comes out right
        # regardless of the station's bitrate.
        req = urllib.request.Request(url, headers={"User-Agent": RADIO_UA})
        with urllib.request.urlopen(req, timeout=10) as upstream:
            with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
                tmp_path = tmp.name
                start = time.time()
                total = 0
                while time.time() - start < 15 and total < 5_000_000:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    total += len(chunk)

        try:
            artist, title, score = _fingerprint_lookup(tmp_path, api_key, fpcalc_path, timeout=20)
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)})
        if title is None:
            return jsonify({"ok": False, "error": "no_match"})
        return jsonify({"ok": True, "artist": artist, "title": title, "score": score})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            os.remove(tmp_path)


# Server-side VU/spectrum levels for internet radio. Web Audio's
# AnalyserNode -- what normally drives the Hi-Fi needle and Cassette
# spectrum bars -- turns out to have a real, long-standing WebKit bug
# (bugs.webkit.org #180696, #211394 and others) where createMediaElementSource
# never produces usable data for network-streamed audio at all, no matter
# how or when it's set up; local file playback is unaffected, which is
# exactly the split reported. Rather than rely on the browser to analyze a
# live stream, ffmpeg decodes a second, independent connection to the same
# station to raw PCM here, and the frontend polls for the numbers instead of
# reading them out of an AnalyserNode.
RADIO_LEVELS_BANDS = 12
RADIO_LEVELS_WINDOW = 256  # samples per FFT window
_radio_levels_state = {}
_radio_levels_lock = threading.Lock()


def _fft(x):
    n = len(x)
    if n <= 1:
        return x
    even = _fft(x[0::2])
    odd = _fft(x[1::2])
    combined = [0j] * n
    for k in range(n // 2):
        t = cmath.exp(-2j * cmath.pi * k / n) * odd[k]
        combined[k] = even[k] + t
        combined[k + n // 2] = even[k] - t
    return combined


def _kill_ffmpeg(proc):
    """proc.terminate() (SIGTERM) alone reliably leaves this ffmpeg running
    -- it's normally blocked writing to a stdout pipe nobody's draining
    anymore by the time this is called, and doesn't act on SIGTERM promptly
    from that state. There's nothing worth flushing gracefully in a
    throwaway analysis feed, so go straight to SIGKILL."""
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


def _radio_levels_worker(sid, url, ffmpeg_path):
    proc = None
    try:
        proc = subprocess.Popen(
            [ffmpeg_path, "-user_agent", RADIO_UA, "-i", url,
             "-f", "s16le", "-ar", "22050", "-ac", "1", "-loglevel", "quiet", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        with _radio_levels_lock:
            if sid not in _radio_levels_state:
                _kill_ffmpeg(proc)
                return
            _radio_levels_state[sid]["process"] = proc

        chunk_bytes = 22050 * 2 // 10  # ~100ms of mono 16-bit PCM
        while True:
            with _radio_levels_lock:
                entry = _radio_levels_state.get(sid)
                if not entry:
                    break
                # A client that stopped polling (closed the app, switched
                # away without the stop call reaching us) shouldn't leave
                # ffmpeg running forever -- self-terminate once idle.
                if time.time() - entry["last_poll"] > 15:
                    break

            t0 = time.time()
            data = proc.stdout.read(chunk_bytes)
            if not data or len(data) < 4:
                break
            n = len(data) // 2
            samples = struct.unpack(f"<{n}h", data[:n * 2])

            rms = min(1.0, (sum(s * s for s in samples) / len(samples)) ** 0.5 / 32768)

            window = list(samples[:RADIO_LEVELS_WINDOW]) + [0] * max(0, RADIO_LEVELS_WINDOW - len(samples))
            mags = [abs(v) for v in _fft(window)[:RADIO_LEVELS_WINDOW // 2]]
            peak = max(mags) or 1.0
            per_band = len(mags) // RADIO_LEVELS_BANDS
            bands = [
                min(1.0, sum(mags[i * per_band:(i + 1) * per_band]) / per_band / peak)
                for i in range(RADIO_LEVELS_BANDS)
            ]

            with _radio_levels_lock:
                if sid not in _radio_levels_state:
                    break
                _radio_levels_state[sid]["level"] = rms
                _radio_levels_state[sid]["bands"] = bands

            elapsed = time.time() - t0
            if elapsed < 0.1:
                time.sleep(0.1 - elapsed)
    except Exception:
        pass
    finally:
        with _radio_levels_lock:
            _radio_levels_state.pop(sid, None)
        if proc:
            _kill_ffmpeg(proc)


@bp.route("/api/radio/levels/start", methods=["POST"])
def radio_levels_start():
    ffmpeg_path = _find_binary("ffmpeg")
    if not ffmpeg_path:
        return jsonify({"ok": False, "error": "ffmpeg_missing"})

    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "")
    sid = data.get("sid", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not sid:
        abort(400)

    with _radio_levels_lock:
        # Single-active-station app -- stop any other running level workers
        # so switching stations can't leak ffmpeg processes.
        for old_sid, entry in list(_radio_levels_state.items()):
            if old_sid == sid:
                continue
            proc = entry.get("process")
            if proc:
                _kill_ffmpeg(proc)
            _radio_levels_state.pop(old_sid, None)

        if sid in _radio_levels_state:
            return jsonify({"ok": True})
        _radio_levels_state[sid] = {
            "level": 0.0, "bands": [0.0] * RADIO_LEVELS_BANDS,
            "process": None, "last_poll": time.time(),
        }

    threading.Thread(target=_radio_levels_worker, args=(sid, url, ffmpeg_path), daemon=True).start()
    return jsonify({"ok": True})


@bp.route("/api/radio/levels")
def radio_levels():
    sid = request.args.get("sid", "")
    with _radio_levels_lock:
        entry = _radio_levels_state.get(sid)
        if not entry:
            return jsonify({"ok": False})
        entry["last_poll"] = time.time()
        return jsonify({"ok": True, "level": entry["level"], "bands": entry["bands"]})


@bp.route("/api/radio/levels/stop", methods=["POST"])
def radio_levels_stop():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    with _radio_levels_lock:
        entry = _radio_levels_state.pop(sid, None)
    if entry and entry.get("process"):
        _kill_ffmpeg(entry["process"])
    return jsonify({"ok": True})


@bp.route("/api/radio/levels/stop-all", methods=["POST"])
def radio_levels_stop_all():
    """Called from the desktop shell's window-closing handler, alongside
    the existing auto-backup call -- a normal app quit while radio is
    playing would otherwise leave its ffmpeg decode running until the 15s
    idle timeout catches it on its own."""
    with _radio_levels_lock:
        entries = list(_radio_levels_state.values())
        _radio_levels_state.clear()
    for entry in entries:
        if entry.get("process"):
            _kill_ffmpeg(entry["process"])
    return jsonify({"ok": True})
