"""Cover-art lookup across multiple free, keyless sources, tried in order
until one finds a real match. Deezer (already used elsewhere in this app
for genre/year lookups) covers most mainstream tracks, but misses plenty
of real ones -- classical recordings, obscure remixes, older/regional
releases. iTunes Search and MusicBrainz+Cover Art Archive fill in a good
chunk of what Deezer alone doesn't have, and neither needs an API key
(unlike Last.fm/Spotify, which would need the user to go get one first).

Shared by app.py's _fetch_and_cache_art (the per-track "Fetch cover art"
button and the bulk iPod-import art safety net) and ipod_import.py's
fix_staged_art, so both get the same, single fallback chain instead of
Deezer-only logic duplicated in two places.
"""
import json
import time
import urllib.parse
import urllib.request

DEEZER_SEARCH = "https://api.deezer.com/search"
ITUNES_SEARCH = "https://itunes.apple.com/search"
MUSICBRAINZ_SEARCH = "https://musicbrainz.org/ws/2/recording"
COVER_ART_ARCHIVE = "https://coverartarchive.org/release"

# MusicBrainz's own usage policy asks for a real identifying User-Agent
# (unlike Deezer/iTunes, which don't check) -- an unset/generic one risks
# getting rate-limited or blocked outright.
_MUSICBRAINZ_HEADERS = {"User-Agent": "NotoriousBPM/1.0 (local desktop music library app)"}


def _http_json(url, params=None, headers=None, retries=2):
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(full_url, headers=headers or {})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(1.0)
    return None


def _deezer_cover_url(artist, title):
    # Deezer's quoted artist:"X" track:"Y" advanced-filter syntax now
    # reliably returns zero results (verified against several well-known
    # tracks) -- a plain query plus filtering candidates down to the same
    # artist is what actually works.
    results = _http_json(DEEZER_SEARCH, {"q": f"{(artist or '').strip()} {(title or '').strip()}".strip(), "limit": 5})
    candidates = (results or {}).get("data") or []
    artist_lower = (artist or "").strip().lower()
    same_artist = [
        c for c in candidates
        if artist_lower and artist_lower in (c.get("artist", {}).get("name") or "").strip().lower()
    ]
    best = (same_artist or candidates or [None])[0]
    return best.get("album", {}).get("cover_big") if best else None


def _itunes_cover_url(artist, title):
    results = _http_json(ITUNES_SEARCH, {
        "term": f"{(artist or '').strip()} {(title or '').strip()}".strip(),
        "media": "music", "entity": "song", "limit": 5,
    })
    candidates = (results or {}).get("results") or []
    artist_lower = (artist or "").strip().lower()
    same_artist = [c for c in candidates if artist_lower and artist_lower in (c.get("artistName") or "").strip().lower()]
    best = (same_artist or candidates or [None])[0]
    art100 = best.get("artworkUrl100") if best else None
    # artworkUrl100 is a 100x100 thumbnail -- iTunes serves any size via
    # the same path, just swapping the dimensions token in the filename.
    return art100.replace("100x100bb", "1200x1200bb") if art100 else None


def _musicbrainz_cover_url(artist, title):
    safe_artist = (artist or "").strip().replace('"', "")
    safe_title = (title or "").strip().replace('"', "")
    if not safe_artist or not safe_title:
        return None
    query = f'artist:"{safe_artist}" AND recording:"{safe_title}"'
    results = _http_json(
        MUSICBRAINZ_SEARCH, {"query": query, "fmt": "json", "limit": 5}, headers=_MUSICBRAINZ_HEADERS,
    )
    recordings = (results or {}).get("recordings") or []
    for rec in recordings[:3]:
        for release in (rec.get("releases") or [])[:2]:
            release_id = release.get("id")
            if not release_id:
                continue
            time.sleep(1.0)  # MusicBrainz/Cover Art Archive courtesy rate limit
            req = urllib.request.Request(f"{COVER_ART_ARCHIVE}/{release_id}/front-500", headers=_MUSICBRAINZ_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        return req.full_url
            except Exception:
                continue
    return None


# Deezer first (fastest, covers the most mainstream tracks with a single
# request); iTunes next (also one request, different catalog gaps); only
# reaches MusicBrainz -- slower, two round trips plus its own rate limit --
# for whatever's left, since it also covers real ground Deezer/iTunes miss
# (classical, older/regional releases, obscure remixes). Looked up by name
# through globals() at call time, not bound directly in this tuple, so
# tests can mock.patch.object(art_lookup, "_deezer_cover_url", ...) and
# actually have it take effect.
_SOURCES = (
    ("deezer", "_deezer_cover_url"),
    ("itunes", "_itunes_cover_url"),
    ("musicbrainz", "_musicbrainz_cover_url"),
)


def find_cover_url(artist, title):
    """Returns (cover_url, source_name), or (None, None) if nothing in any
    source matched."""
    for name, lookup_name in _SOURCES:
        try:
            url = globals()[lookup_name](artist, title)
        except Exception:
            url = None
        if url:
            return url, name
    return None, None
