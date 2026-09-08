# Notorious B.P.M.

A local music library manager that doesn't look like one. Point it at a
folder of music and it becomes a full Hi-Fi receiver with real VU meters, a
cassette deck, a turntable, or a clean minimal player — your choice, switch
anytime. Underneath the theatrics it's a genuinely useful library tool:
tag repair, duplicate cleanup, artist-folder organization, smart playlists,
and live internet radio with genre-matching and Shazam-style song ID.

Runs as a native desktop app (no browser tab, no cloud, no account) on
macOS, Windows, and Linux.

## Features

**Library**
- Fast local scan (MP3/FLAC/M4A/WAV/OGG), auto-tagging fallback from
  filenames, language detection
- Search, filter by genre/decade/artist/language/rating, regular and
  smart (rule-based, always-live) playlists
- Tag checker with one-click Deezer genre lookup; duplicate
  (Live/Remastered/Mix) detection and cleanup
- Organize by Artist — physically sorts files on disk into per-artist
  folders, preserving ratings and playlists
- Format conversion, trash (soft delete), automatic rating/playlist
  backup on every quit

**Player themes**
- **Default** / **Graphite** — clean, minimal
- **Hi-Fi** — a full stereo receiver face with audio-reactive VU meters
- **Cassette** — a deck with a spinning tape and spectrum bars, several
  shell designs
- **Vinyl** — a turntable with a moving tonearm, multiple wood finishes

**Live Radio**
- Browse and play public internet radio stations (via the free
  [Radio Browser](https://www.radio-browser.info/) directory)
- **Match My Library** — finds stations playing music similar to your
  library's most common genre, automatically
- **Identify this song** — Shazam-style recognition for stations that
  don't report what's playing, via free/open
  [AcoustID](https://acoustid.org/) fingerprinting
- Lyrics lookup works for radio too, once a track is identified

## Installing

Pre-built installers aren't part of this repo (see `.gitignore`) — build
from source for your platform:

| Platform | Guide |
|---|---|
| macOS | `./build_macos.sh` (see its header comment) |
| Windows | [`BUILD_WINDOWS.md`](BUILD_WINDOWS.md) |
| Linux | [`BUILD_LINUX.md`](BUILD_LINUX.md) |

All three are self-contained builds — no separate Python install needed
for whoever runs the built app, only for building it.

### Optional extras

Two features are optional and self-detect at runtime — the app works
fully without them, these just unlock a couple of Live Radio features:

- **`ffmpeg`** — powers the Hi-Fi/Cassette themes' VU meters and spectrum
  bars *for internet radio specifically* (local file playback doesn't
  need it).
- **`chromaprint`** (provides `fpcalc`) — powers "Identify this song".
  Needs a free [AcoustID](https://acoustid.org/new-application) API key,
  which the app prompts for the first time you use it.

## Development

No build step for the frontend — `static/` is plain HTML/CSS/JS served
directly by the Flask backend in `app.py`. Run it straight from source:

```
pip install -r requirements.txt
python3 launcher.py
```

## License

Donationware — free to use, not for resale or redistribution under a
different name. See [`LICENSE`](LICENSE) for the full terms. If you're
enjoying it, donations are welcome (never required) at
[paypal.me/IPecek](https://paypal.me/IPecek).
