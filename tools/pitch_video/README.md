# Pitch video pipeline

Builds the Notorious B.P.M. promo/pitch video (and its YouTube thumbnail, captions and description) from the real
app, with no screen recording: a headless Chrome is driven over its own DevTools websocket, so nothing on your
real screen is ever captured.

```
python3 make_video.py            # 1080p, ~15 min
python3 make_video.py --draft    # 960x540, to check timing and wiring
```

Output lands in `work/out/` (override the folder with `PITCH_WORK=/some/path`): `pitch.mp4`, `pitch.srt`
(captions), `thumbnail.jpg`. Copy `youtube.txt`-style text from the last run's description if you want chapters;
`out/timeline.json` has every beat's start time.

## Needs
Google Chrome, `ffmpeg`/`ffprobe`, `fpcalc` (chromaprint, for the duplicates scene), `pip install websockets`, and
the macOS voice **Daniel (Enhanced)** (System Settings ▸ Accessibility ▸ Spoken Content ▸ Manage Voices) for the
narration. Edit `RATE`/`VOICE` and the script in `narration.py`.

## How it works
| file | role |
|---|---|
| `setup_library.py` | demo library under `work/`: a tree of **symlinks** to your music (`PITCH_SOURCE_MUSIC`, default `~/Music/ALAC`), four held-back tracks for the import scene, four re-encoded duplicates |
| `serve.py` | the app on port 5788 against that scratch library and a scratch config |
| `cap.py`, `lib.py` | headless-Chrome driver, synthetic cursor/captions/keycaps, fake playback state, motion frames |
| `scenes.py` | one function per scene; each records its frames and how they lay out in time (`manifests/`) |
| `cards.py`, `thumb.py` | hook/title/trust/closing cards and the thumbnail (real covers from the library) |
| `narration.py` | the script; renders it with `say` and measures each line |
| `assemble.py` | per-beat clips timed to the speech, cross-fades, narration mixed at exact offsets, loudness-normalised, captions |
| `make_video.py` | runs everything in the right order |

Order matters, and `make_video.py` encodes it: the clean-library scenes are shot first; then the duplicate files are
added, fingerprints cleared and the server restarted (it remembers a finished duplicate scan in memory, and the
panel should open in its never-run state) before the duplicate/tag/undo/import scenes.

## Safety
The demo tracks are symlinks to real files. **Never** call an endpoint that writes tags or moves/deletes files on
them (`/api/tags`, organize, delete, convert...). The undo scene only edits the scratch re-encodes under
`music/_Downloads`, and asserts so before it does. Playback is never started (headless Chrome wedges on local
audio); the player skins' motion is drawn from the app's own render code with a pretend playhead, which the video
description says.

Edit the artist/track names at the top of `setup_library.py` and `SHOWCASE` in `scenes.py` for a different library.
