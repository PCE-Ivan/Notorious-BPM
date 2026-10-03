# Notorious B.P.M.

A local music library manager that doesn't look like one. Point it at a
folder of music and it becomes a Hi-Fi receiver with real VU meters, a
cassette deck, a turntable, or a clean minimal player — your choice, switch
anytime. Underneath the theatrics it's a genuinely useful library tool:
duplicate finding by sound, tag repair with one-click undo, drag-and-drop
import, artist-folder organization, smart playlists, and live internet radio
with Shazam-style song ID.

A native Mac app: no browser tab, no cloud, no account.

## Download

**[Get the latest DMG from Releases →](https://github.com/PCE-Ivan/Notorious-BPM/releases/latest)**

Needs a Mac with **Apple Silicon** (M1 or later) running **macOS 14 or newer**.

1. Open the DMG and drag *Notorious BPM* into Applications.
2. The app is signed locally, not notarized by Apple, so on first launch macOS
   may say it is "damaged" or can't be verified. It isn't — run this once in
   Terminal, then open the app normally:

   ```
   xattr -cr "/Applications/Notorious BPM.app"
   ```

   (or: System Settings ▸ Privacy & Security ▸ scroll down ▸ **Open Anyway**).
3. The first time it reads your music folder, macOS asks whether the app may
   access it (Desktop, Documents, an external drive…). Click **Allow**. If you
   miss the dialog, the app says so and offers a shortcut to Privacy settings.

## Features

**Library**
- Fast local scan (MP3/FLAC/M4A/WAV/OGG), tag fallback from filenames,
  language detection; thousands of tracks stay smooth
- Tracks, Albums and Artists views with cover art; instant search; filter by
  genre/decade/artist/language/rating; regular and smart (rule-based,
  always-live) playlists
- Keyboard-first browsing (J/K, Enter, X, shift-click ranges — see
  Help ▸ Keyboard)
- Add music by dragging files or whole folders onto the window (copied into
  `<music>/<Artist>/`, duplicates skipped)
- Multiple independent libraries, each one `.nbpmlib` file you can keep on any drive

**Cleaning up**
- Find duplicates **by sound** — compares audio fingerprints locally (no
  network), so renamed or re-tagged copies are found too; marks the best copy
  and sends the rest to a Trash you can restore from. Cached, so only the
  first run is slow
- Tag checker with one-click Deezer genre/year lookup; verify tags against the
  audio via AcoustID, for the whole library, resumable
- Change history with one-click **Undo** for bulk tag edits and file moves;
  library health check against the disk
- Organize by Artist — physically sorts files into per-artist folders,
  preserving ratings and playlists
- Format conversion (FLAC, ALAC, MP3), iPod Classic import

**Playback**
- Player themes: **Default** / **Graphite** (or *Match system* light/dark),
  **Hi-Fi** (audio-reactive VU meters), **Cassette** (spinning tape, spectrum
  bars), **Vinyl** (turntable with a moving tonearm, several wood finishes)
- AirPlay output; "level volume across tracks" (ReplayGain-style, opt-in)
- **Live Radio**: public internet stations (via the free
  [Radio Browser](https://www.radio-browser.info/) directory), **Match My
  Library**, and **Identify this song** — Shazam-style recognition via free,
  open [AcoustID](https://acoustid.org/) fingerprinting; lyrics lookup too

**Reliability**
- Every long task is a cancellable background job (Activity tray)
- Readable errors, rotating logs, a "Copy diagnostics" button in About
- Notices an unplugged drive or a macOS folder-permission block and says so
- Automatic rating/playlist backup on every quit

### Optional extras

These self-detect at runtime — the app works without them:

- **`ffmpeg`** — loudness measurement for volume leveling, and VU meters for
  internet radio.
- **`chromaprint`** (provides `fpcalc`) — finding duplicates by sound and
  "Identify this song". Radio ID also needs a free
  [AcoustID](https://acoustid.org/new-application) API key, which the app
  prompts for the first time you use it.

Install both with `brew install ffmpeg chromaprint`.

## Building from source

```
pip install -r requirements.txt
python3 desktop_macos.py          # run it from source
./build_macos.sh                  # build the .app and the DMG (needs create-dmg)
```

`./build_macos.sh` signs ad-hoc with a pinned requirement; run
`./setup_signing.sh` once for a stable local signing certificate, so macOS
keeps your folder-access grants across rebuilds.

Tests: `./run_tests.sh` runs every `test_*.py` in its own process with `HOME`
pointed at a throwaway directory (never combine test files in one process —
`app.py` fixes its library paths when first imported). Tests only ever touch
scratch libraries.

`static/` is plain HTML/CSS/JS served by the Flask backend in `app.py`, split
by feature (`app.js` core + player, `ipod.js`, `tagcheck.js`, `duplicates.js`,
`radio.js`, `browse.js`, `keys.js`, `activity.js`, `import.js`, `health.js`,
`history.js`) — classic scripts sharing one global scope, loaded in the order
`index.html` lists them. `test_frontend_syntax.py` and `test_frontend_ids.py`
catch a script that doesn't parse or an `el("id")` with no matching element.

`tools/pitch_video/` builds the promo video from the real app (see its README).

## License

Donationware — free to use, not for resale or redistribution under a
different name. See [`LICENSE`](LICENSE) for the full terms. If you're
enjoying it, donations are welcome (never required) at
[paypal.me/IPecek](https://paypal.me/IPecek).
