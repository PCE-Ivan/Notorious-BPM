# Running (and optionally building) Notorious B.P.M. on Linux

I can't build or test this myself — I'm running on macOS with no Linux
machine available — so this is everything needed to get it running on a
Linux machine, plus what to check along the way. The good news: almost all
of the app's code is already fully cross-platform (it already picks the
right config folder, file dialogs, etc. for whichever OS it's running on)
— nothing in `app.py`, `config.py`, `scan_library.py`, or `launcher.py`
needed to change for Linux at all. The only Linux-specific things are the
system packages pywebview needs for its native window, listed below.

These instructions assume **Ubuntu or Debian** (the most common case for
"a friend wants to test it"). If it's a different distro (Fedora, Arch,
etc.), the package names below will differ — search your distro's package
manager for `webkit2gtk` and `python3-gi` equivalents.

## Part 1 — Just run it (recommended first step)

This runs the app directly with Python, no building/packaging at all. It's
the fastest way to confirm it actually works before bothering with a real
standalone build.

**1. Install system packages** (these provide the native window toolkit —
they can't come from `pip`, they have to come from the OS):

```
sudo apt update
sudo apt install python3 python3-pip python3-venv python3-tk \
  python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1 \
  ffmpeg libchromaprint-tools
```

- `python3-tk` — the folder-picker dialog on first run.
- `python3-gi` / `gir1.2-gtk-3.0` / `gir1.2-webkit2-4.1` — pywebview's
  native window (GTK + WebKit2GTK backend). If `gir1.2-webkit2-4.1` isn't
  found, try `gir1.2-webkit2-4.0` instead — the package was renamed between
  Ubuntu versions.
- `ffmpeg` / `libchromaprint-tools` — only needed for the Live Radio
  feature's VU meters and the "Identify this song" button. The rest of the
  app works fine without them; those two features just quietly report
  "not available" if missing.

**2. Set up a virtual environment and install the Python dependencies.**
`--system-site-packages` is important here — it's what lets the venv see
the `python3-gi` system package from step 1, since that one specifically
can't be installed via pip:

```
cd notorious-bpm    # this folder
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install -r requirements-linux.txt
```

**3. Run it:**

```
python3 launcher.py
```

It should prompt for a music folder, index it, and open in its own native
window (title bar, no browser chrome). If something goes wrong, run it
exactly like that from a terminal (not double-clicked) — any error will
print right there, which is the thing to send back for help diagnosing.

Common issues:
- **`ModuleNotFoundError: No module named 'gi'`** — the venv wasn't created
  with `--system-site-packages`, or `python3-gi` wasn't actually installed.
  Re-check step 1 and recreate the venv.
- **A browser tab opens instead of a native window** — that's the app's
  own fallback kicking in because pywebview's GTK backend couldn't
  initialize (still fully usable, just not a native-looking window). The
  terminal output will have a line starting "Native window unavailable"
  naming the actual error — that's the real thing to chase down.
- **Nothing happens / instantly exits** — run it from a terminal (not a
  file manager double-click) to see the actual error.

## Part 2 (optional) — Build a real standalone binary

Once Part 1 works, this bundles Python and every dependency into one
folder so it can run on another Linux machine without installing Python or
any of this at all (the GTK/WebKit2 system packages from step 1 above are
still needed on whatever machine runs the built binary, though — those are
OS-level, PyInstaller can't bundle them).

```
pyinstaller jukebox_linux.spec
```

This produces `dist/NotoriousBPM/NotoriousBPM` — an executable — plus a
folder of supporting files it needs next to it. That whole
`dist/NotoriousBPM/` folder is the app; copy the entire folder, not just
the executable inside it.

Test it before sharing it further:

```
./dist/NotoriousBPM/NotoriousBPM
```

If it can't find a hidden import, PyInstaller's error will name the
missing module — add it to the `hiddenimports` list near the top of
`jukebox_linux.spec` and rebuild.

## Part 3 (optional) — Package as an AppImage

An AppImage is a single file that runs on most Linux distros without
installing anything (no root, no package manager) — the closest Linux
equivalent to a plain `.app` or `.exe` you can just hand someone. Worth
doing once Part 2's build works, if you want something easy to share
rather than a folder.

```
# One-time: get the AppImage packaging tool
wget https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x appimagetool-x86_64.AppImage

# Build the AppDir structure around the PyInstaller output
mkdir -p NotoriousBPM.AppDir/usr/bin
cp -r dist/NotoriousBPM/* NotoriousBPM.AppDir/usr/bin/
cat > NotoriousBPM.AppDir/NotoriousBPM.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Notorious B.P.M.
Exec=NotoriousBPM
Icon=notorious-bpm
Categories=AudioVideo;Audio;Player;
EOF
# Needs a 256x256 (or similar) PNG icon named notorious-bpm.png in the
# AppDir root -- any square logo works, this repo doesn't have one ready
# for Linux specifically (icon.ico from the Windows kit would need
# converting: `convert icon.ico -resize 256x256 notorious-bpm.png`
# using ImageMagick, if you have a copy of that file handy).
cp NotoriousBPM.AppDir/usr/bin/NotoriousBPM NotoriousBPM.AppDir/AppRun  # or a small wrapper script

./appimagetool-x86_64.AppImage NotoriousBPM.AppDir
```

This produces a single `Notorious_B.P.M.-x86_64.AppImage` file — mark it
executable (`chmod +x`) and double-click or run it directly, no
installation step at all. This part is the least tested of the three
(genuinely untried, not just "untested by me on Linux" like the rest) —
if `appimagetool` complains about the AppDir structure, its own error
messages are more authoritative than this guide.
