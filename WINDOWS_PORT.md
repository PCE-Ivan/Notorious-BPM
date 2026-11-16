# Building Notorious B.P.M. for Windows

A brief for whoever (person or Claude session) builds the Windows version on a
Windows PC. The macOS app at this commit is the reference: the Windows build
should do everything it does, except where "Cannot come across" below says so.

## The goal

A normal Windows installer (`Notorious-BPM-Setup-<version>.exe`) that installs
the app with a Start-menu entry and an uninstaller, runs with nothing else to
install, and behaves like the Mac app. Version number: the one in `version.py`.

## Starting point: the old Windows build is in git history

The repo had a working Windows build until commit `6281e44` removed it (the
project went macOS-only for a while). Restore those files rather than starting
from nothing:

```
git checkout 6281e44^ -- BUILD_WINDOWS.md jukebox.spec launcher.py installer.iss fix_pywebview_windows.py runtimeconfig.json assets/AppIcon.ico
```

- `launcher.py` — the Windows desktop launcher (pywebview on WebView2)
- `jukebox.spec` — the PyInstaller spec
- `installer.iss` — the Inno Setup script
- `fix_pywebview_windows.py`, `runtimeconfig.json` — pythonnet/pywebview fixes
- `BUILD_WINDOWS.md` — the old build instructions (read first)
- `requirements.txt` also listed `pythonnet` then; add it back for Windows

They are several versions behind. Everything below has been added to the Mac
side since and must be carried over.

## What the Mac launcher does that the Windows one must match

Read `desktop_macos.py` side by side with the restored `launcher.py`:

- Checks `/api/instance` before attaching to a server already on the port
- Native menu bar (`_build_menu`) — on Windows, a window menu or none; every
  item must still be reachable from the page
- Drag-and-drop of files/folders onto the window -> `/api/import/files`
  (`_bind_file_drop`, `_dropped_paths`, `_send_to_import`)
- Focus mode / pop-out window sizing (`enter_focus` and friends)
- `open_external_url`, `save_export` bridge functions used by the page
- Stops radio level meters on quit (`/api/radio/levels/stop-all`)
- The macOS folder-permission pre-touch is Mac-only: skip it on Windows

## PyInstaller: modules to include

`jukebox_macos.spec` has the current list of `hiddenimports`. The Windows spec
needs the same app modules, including the ones added since: `audio_dupes`,
`loudness`, `importer`, `local_guard`, `filename_cleanup`, `rating_tags`,
`version`, `radio_routes`. A missing one means the built app won't start even
though running from source works — always test the built `.exe`, not source.

## Mac-only code paths that need a Windows branch

Each of these checks `sys.platform == "darwin"`; give it a Windows equivalent
and test it:

| Where | What | Windows equivalent |
|---|---|---|
| `app.py` reveal track / reveal log (`open -R`) | Show in Finder | `explorer /select,<path>` |
| `app.py` iPod staging reveal (`open`) | open a folder | `os.startfile(path)` |
| `app.py` folder / file pickers (`osascript`) | native dialogs | the tkinter fallback already there, or pywebview's `create_file_dialog` |
| `app.py` library New / Open dialogs | native dialogs | same |
| `system_status.py` | macOS privacy prompts, "Open Privacy Settings" | not needed; make sure the banner never shows on Windows |
| `fs_safety.py` | `rm -f` subprocess fallback | `os.remove` retry, or `cmd /c del` |
| `logging_setup.py`, `config.py` | app data under `~/Library/Application Support` | `%APPDATA%\Notorious BPM` |
| page labels | "Show in Finder", ⌘ in Help | "Show in Explorer", Ctrl |

Search for more: `grep -n "darwin\|osascript\|\"open\"" *.py`.

## Bundle these so nothing needs installing

On the Mac, `ffmpeg` and `fpcalc` come from Homebrew. On Windows, ship both
inside the installer and make `radio_routes._find_binary` look in the app's
own folder first:

- `ffmpeg.exe` — volume leveling, format conversion, radio VU meters
- `fpcalc.exe` (Chromaprint) — duplicates by sound, tag verification, song ID

WebView2 runtime: present on Windows 11 and current Windows 10. The installer
should check and offer the Evergreen bootstrapper if it's missing.

## Cannot come across

- **AirPlay.** It is a Safari/WebKit API; the Windows window runs on Edge's
  engine. The button already hides itself when the API is absent.

Everything else should work. If something else turns out not to be possible,
stop and tell the user rather than quietly dropping it.

## Windows-specific things to get right

- Paths: the library stores paths relative to the music folder using the OS
  separator. A library file made on a Mac won't match on Windows (and vice
  versa) — fine, but say so rather than silently showing an empty library.
- File names: `filename_cleanup._safe_part` already strips `\ / : * ? " < > |`.
  Also guard against reserved names (`CON`, `NUL`, `COM1`…) and trailing dots.
- Long paths (>260 chars) in deep music folders.
- A playing file is locked on Windows: delete-to-Trash, rename (Tidy) and tag
  writes on the track that is currently playing will fail. Handle it with a
  clear message, or stop playback first.
- External drives are letters (`E:\`), not `/Volumes/...`; the "drive is
  unplugged" detection in `system_status.py` needs checking.
- Unsigned installers trigger SmartScreen ("unknown publisher"). Document the
  "More info -> Run anyway" step in the README, as the Mac `xattr` step is.

## Testing — nothing counts until it's done on the PC

1. `python -m unittest` each `test_*.py` in its own process (see
   `run_tests.sh`; write a `run_tests.ps1` that does the same). Some tests
   assume macOS (`test_system_status.py`, `test_desktop_launcher.py`): port
   them or mark them Mac-only, don't delete them.
2. Tests must only ever touch scratch libraries (`JUKEBOX_DB_PATH`,
   `JUKEBOX_CONFIG_PATH`, `JUKEBOX_MUSIC_DIR`), never the user's real one.
3. Then, in the **installed** app, by hand, with a scratch music folder:

- [ ] First run: pick a folder, scan, tracks appear with cover art
- [ ] Play / pause / seek / next / previous; shuffle, repeat, queue reorder
- [ ] Quit and reopen: track, position, queue come back, paused
- [ ] Sleep timer fades out; "end of track" stops
- [ ] All five themes; VU meters move; Compact and Comfortable layouts
- [ ] Sort by each column; Albums and Artists grids; search; filter chips
- [ ] Rate one track, rate a selection, delete a selection, restore from Trash
- [ ] Edit tags; History -> Undo puts them back
- [ ] Rescan after renaming a file in Explorer: rating kept
- [ ] Tidy file names (copy numbers and a pattern), then Undo
- [ ] Save ratings inside files: on, rate, check the tag in another player
- [ ] Find duplicates (by tags and by sound); remove; review
- [ ] Organize by Artist, then Undo
- [ ] Drag a folder of music onto the window: imported
- [ ] Convert a track to MP3/FLAC
- [ ] Live Radio: play a station, now-playing title, Identify this song
- [ ] Volume leveling; lyrics; fetch cover art
- [ ] Multiple libraries: new, open, switch
- [ ] Backup export / import
- [ ] Health check; Stats; About shows the version and the update check
- [ ] Unplug the music drive while running: clear message, no crash
- [ ] Uninstall leaves the music and the library file alone

## Releasing

Ask the user before publishing anything. When they say so, add the installer
to the GitHub release for that version alongside the Mac DMG, with its
SHA-256 in the notes, and put a Windows section back in `README.md`.
