# Building the Notorious B.P.M. Windows installer

This produces a real `NotoriousBPMSetup.exe` you can hand to anyone — no Python
install required on their end, since PyInstaller bundles the Python runtime
and all dependencies into the app itself.

I can't build or test this myself (I'm running on macOS with no Windows
available), so this is everything needed to do it on a Windows machine, plus
what to check along the way.

## What you need on the Windows machine

1. **Python 3.10+** (from python.org — check "Add python.exe to PATH" during
   install).
2. **[Inno Setup](https://jrsoftware.org/isdl.php)** (free) — this is what
   turns the built app into a proper `Setup.exe` installer.
3. This whole `jukebox-app` folder, copied over (USB drive, cloud sync,
   git clone — whatever's easiest).

## Steps

Open a terminal (Command Prompt or PowerShell) in the `jukebox-app` folder.

**1. Install dependencies:**
```
pip install -r requirements.txt
```

**2. Build the app with PyInstaller:**
```
pyinstaller jukebox.spec
```
This produces `dist\NotoriousBPM\NotoriousBPM.exe` plus a folder of supporting files
next to it — that whole `dist\NotoriousBPM\` folder is the app.

**3. Test it before packaging.** Double-click `dist\NotoriousBPM\NotoriousBPM.exe`
directly. It should prompt you to pick a music folder, index it, and open
in its own native window (title bar, taskbar entry — no browser chrome). If
it doesn't:
- If nothing happens at all, temporarily edit `jukebox.spec` and set
  `console=True`, rebuild, and run it from a terminal instead of
  double-clicking — that gets you a visible error/traceback instead of a
  silent failure.
- A `ModuleNotFoundError` for some package means it needs adding to the
  `hiddenimports` list near the top of `jukebox.spec`, then rebuild. This is
  normal PyInstaller behavior, not a sign anything is fundamentally wrong.
- **If a browser tab opens instead of a native window**, that's the
  built-in fallback kicking in — pywebview couldn't initialize (see "Native
  window" below), and it's silently doing the old browser-tab behavior
  instead of crashing. Check the terminal output (with `console=True` as
  above) for a line starting "Native window unavailable" — it names the
  actual error, which is the real thing to chase down (missing WebView2
  runtime, a hidden-import PyInstaller missed, etc.), not a bug in the
  fallback itself.
- Windows Defender / your antivirus may flag or quarantine a freshly-built,
  unsigned .exe the first time — this is a very common PyInstaller false
  positive (unsigned executables from an unrecognized publisher get flagged
  by default), not a real problem with the code. Add an exclusion for the
  `dist` folder while testing.

**Native window (pywebview / WebView2) — untested, most likely thing to
need fixing.** I built this using pywebview with no Windows machine to
verify it on, so this is the part most likely to need a fix-and-rebuild
cycle:
- pywebview's Windows backend (EdgeChromium) needs the **WebView2
  Runtime**. It ships preinstalled on any normal, up-to-date Windows 10/11
  machine (it's an OS component, delivered via Windows Update) — but an
  older, offline, or stripped-down Windows image might not have it. If
  testing turns up a missing-WebView2 error, either install the runtime
  ([evergreen bootstrapper](https://developer.microsoft.com/microsoft-edge/webview2/))
  on the build/test machine, or just leave it — Notorious B.P.M. will keep working
  via the browser-tab fallback either way, just without the native window.
- If the exe runs but the window itself fails to appear (rather than
  falling back cleanly), it's likely PyInstaller missing one of pywebview's
  supporting `.NET` assemblies or a dynamically-loaded submodule.
  `jukebox.spec` already asks PyInstaller to collect `webview`'s package
  data and hidden-imports its known backend modules
  (`webview.platforms.edgechromium`, `clr_loader`, ...), but pywebview's
  exact packaging can shift between versions — if something's still
  missing, `console=True` plus the traceback will point at exactly what.

**4. Build the installer.** Open `installer.iss` in the Inno Setup app (or
run `iscc installer.iss` from the command line if you added it to PATH), and
click Compile / Build. The finished installer lands in
`installer_output\NotoriousBPMSetup.exe`.

**5. Test the installer itself** — run it, confirm it installs, creates
Start Menu / Desktop shortcuts, launches correctly, and that uninstalling
cleanly removes what it installed.

## Things worth knowing

- **ffmpeg (format conversion) isn't bundled.** It's a separate binary with
  its own licensing, and pulling it into the installer adds real size and
  complexity. The app works fully without it — only the "Convert" feature
  needs it, and it degrades gracefully with a clear error if it's missing.
  If you want it, the app looks for `ffmpeg.exe` on PATH or in a couple of
  common install spots (see `convert_audio.py`); installing it via
  `winget install ffmpeg` (or Chocolatey / Scoop) is enough.

- **Closing the window quits the app** — in the normal native-window case,
  the server runs on a background thread of the same process as the
  window, so closing it ends the process cleanly, same as any other
  desktop app. No system tray icon, no "keep running in the background"
  behavior to worry about.
  The one place the old "runs forever until you kill it in Task Manager"
  behavior still applies is the **browser-tab fallback** (pywebview
  couldn't initialize) — there's no window to close as a quit signal there,
  so it keeps the server running until you end `NotoriousBPM.exe` in Task
  Manager, same as the very first version of this build. A system tray icon
  with a real Quit option would fix that path too, if it ever comes up in
  practice — just ask.

- **Where the app's data lives**: `%AppData%\Jukebox\` — the library index,
  your ratings/playlists, cached art, trash, and DB backups. Still named
  `Jukebox`, deliberately, even after the rename to Notorious B.P.M. --
  changing it would orphan anyone's existing library index/ratings/
  playlists with no migration path, for a folder name nobody actually sees
  in normal use. Your actual music files are never touched except by things
  you explicitly ask for (rating-based delete, duplicate cleanup, tag
  edits) — and even then, "deleted" files move to
  `%AppData%\Jukebox\trash\`, not gone for good.

- **The app icon**: drop `AppIcon.ico` (in `assets/` in the main repo) into
  this same folder as `icon.ico` before running PyInstaller --
  `jukebox.spec` picks it up automatically if present (`icon_path =
  "icon.ico" if os.path.isfile("icon.ico") else None`), and silently skips
  it (unbranded default icon) if it's missing. Untested on real Windows --
  if the .ico doesn't show up on the built .exe, double-check it's a real
  multi-resolution .ico (not a renamed .png) sitting right next to
  `jukebox.spec`, not inside a subfolder.

- **Re-running this build** after future code changes: just repeat steps 2
  and 4 (`pyinstaller jukebox.spec` then recompile `installer.iss`) — no
  need to reinstall dependencies unless `requirements.txt` changed.
