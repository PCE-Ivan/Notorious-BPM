# Building the Notorious B.P.M. Windows installer

This produces a real `NotoriousBPMSetup.exe` you can hand to anyone — no
Python install required on their end, since PyInstaller bundles the Python
runtime and all dependencies into the app itself.

This has now actually been built and tested end-to-end on a real Windows
machine (an ARM64 Windows 10 VM) — running from source, a PyInstaller
standalone build, the real installer produced by `installer.iss`, the
native window, and the Hi-Fi/Cassette VU meters reacting to local file
playback are all confirmed working. Getting the native window working
took real digging (see "Fixing the native window" below) — a handful of
genuine bugs in how pywebview's Windows backend interacts with a plain,
from-scratch .NET install, not this app's own code.

**Architecture note:** PyInstaller builds for whatever CPU architecture the
machine running it has — there's no cross-compiling. An ARM64 build (like
the one actually tested) only runs on ARM64 Windows (Surface Pro X-class
devices, ARM laptops) — not a normal Intel/AMD PC. Building for x86/x64
needs its own separate build on an x86/x64 Windows machine; `installer.iss`
has a comment at `ArchitecturesAllowed`/`ArchitecturesInstallIn64BitMode`
marking the one line to change for that.

## What you need on the Windows machine

1. **Python 3.10+** (from python.org — check "Add python.exe to PATH"
   during install).
2. **The .NET Desktop Runtime** (free,
   [download](https://dotnet.microsoft.com/download/dotnet/8.0), pick the
   "Desktop Runtime" x64/x86/arm64 installer matching this machine) —
   needed by pywebview's Windows backend to host the native window. Often
   already present (Visual Studio, many games/apps pull it in), but not
   guaranteed on a bare install. `winget install Microsoft.DotNet.DesktopRuntime.8`
   also works.
3. **[Inno Setup](https://jrsoftware.org/isdl.php)** (free,
   `winget install JRSoftware.InnoSetup` also works) — this is what turns
   the built app into a proper `Setup.exe` installer.
4. This whole `jukebox-app` folder, copied over (USB drive, cloud sync,
   git clone — whatever's easiest).

## Steps

Open a terminal (Command Prompt or PowerShell) in the `jukebox-app` folder.

**1. Install dependencies:**
```
pip install -r requirements.txt
python fix_pywebview_windows.py
```
The second line is required, not optional — see "Fixing the native
window" below for what it actually does and why. Safe to re-run any time
(e.g. after upgrading pywebview); it's a no-op if already applied.

**2. Build the app with PyInstaller:**
```
pyinstaller jukebox.spec
```
This produces `dist\NotoriousBPM\NotoriousBPM.exe` plus a folder of
supporting files next to it — that whole `dist\NotoriousBPM\` folder is
the app.

**3. Test it before packaging.** Double-click
`dist\NotoriousBPM\NotoriousBPM.exe` directly. It should prompt you to
pick a music folder, index it, and open in its own native window (title
bar, taskbar entry — no browser chrome). If it doesn't:
- If nothing happens at all, temporarily edit `jukebox.spec` and set
  `console=True`, rebuild, and run it from a terminal instead of
  double-clicking — that gets you a visible error/traceback instead of a
  silent failure.
- A `ModuleNotFoundError` for some package means it needs adding to the
  `hiddenimports` list near the top of `jukebox.spec`, then rebuild. This
  is normal PyInstaller behavior, not a sign anything is fundamentally
  wrong.
- **If a browser tab opens instead of a native window**, that's the
  built-in fallback kicking in — pywebview couldn't initialize. Check the
  terminal output (with `console=True` as above) for a line starting
  "Native window unavailable" — it names the actual error. If you already
  ran `fix_pywebview_windows.py` and installed the .NET Desktop Runtime,
  this shouldn't happen; if it still does, see "Fixing the native window"
  below, the error message will point at one of the same handful of
  causes.
- Windows Defender / your antivirus may flag or quarantine a
  freshly-built, unsigned .exe the first time — this is a very common
  PyInstaller false positive (unsigned executables from an unrecognized
  publisher get flagged by default), not a real problem with the code.
  Add an exclusion for the `dist` folder while testing.

**4. Build the installer.** Open `installer.iss` in the Inno Setup app
(or run `iscc installer.iss` from the command line if you added it to
PATH), and click Compile / Build. The finished installer lands in
`installer_output\NotoriousBPMSetup.exe`.

**5. Test the installer itself** — run it, confirm it installs, creates
Start Menu / Desktop shortcuts, launches correctly, and that uninstalling
cleanly removes what it installed.

## Fixing the native window

pywebview's Windows backend hosts Microsoft Edge WebView2 inside a
WinForms window, using `pythonnet` as the Python↔.NET bridge. On a plain,
from-scratch Windows install, three real, distinct bugs stack up here —
all fixed automatically by `launcher.py` itself or by
`fix_pywebview_windows.py`, but worth understanding since the error
messages are cryptic if any one piece is still missing:

1. **`pythonnet` needs an actual .NET runtime to load, and by default
   only asks for the bare "NETCore" framework, not "WindowsDesktop"** (the
   one that actually contains `System.Windows.Forms`) — so without help,
   loading it throws `Could not load file or assembly
   'System.Windows.Forms'...` the moment pywebview touches WinForms.
   `launcher.py`'s `_init_dotnet_runtime()` explicitly loads `pythonnet`
   with a `runtimeconfig.json` that asks for the WindowsDesktop framework
   specifically, and explicitly pre-loads
   `Microsoft.Win32.SystemEvents` too (a separate assembly under modern
   .NET; it used to live inside `System.Windows.Forms` itself under
   classic .NET Framework, which is what pywebview was actually written
   against). This needs the .NET Desktop Runtime actually installed on
   the machine (see "What you need" above) — `launcher.py` can ask
   `pythonnet` to load the right framework, but can't install it.

2. **pywebview bundles an outdated WebView2 assembly.** Its
   `webview/lib/Microsoft.Web.WebView2.WinForms.dll` (and `.Core.dll`) are
   built for classic .NET Framework (`net462`), and reference a WinForms
   type (`System.Windows.Forms.ContextMenu`) that Microsoft's from-scratch
   .NET Core WinForms port removed entirely — loading it under modern
   .NET throws a `TypeLoadException`. `fix_pywebview_windows.py` swaps in
   the modern (`netcoreapp3.0`-targeted) build of those same two
   assemblies from Microsoft's own WebView2 SDK NuGet package —
   functionally identical API, just built against the right framework
   (and this build genuinely does have working ARM64 native binaries
   already, unlike the vendored ones, which was a separate worry that
   turned out to be a non-issue).

3. **A dead class in pywebview crashes the whole module import.**
   `webview/platforms/winforms.py` has an `OpenFolderDialog` class (this
   app doesn't use it — its own folder picker for choosing the music
   directory is a plain Tkinter dialog) that reflects into a private,
   .NET-Framework-only WinForms implementation detail
   (`System.Windows.Forms.FileDialogNative+IFileDialog`) unconditionally,
   at class-definition time, the moment the module is imported. That type
   doesn't exist under modern .NET's WinForms port, so the lookup returns
   `None`, and the very next line's `.GetMethod(...)` call on that `None`
   throws — taking down the whole module import (and with it, the native
   window) rather than failing gracefully. `fix_pywebview_windows.py`
   patches this class to guard against the type being missing.

If the native window still won't come up after both the runtime install
and the fix script, `console=True` plus the printed traceback (see step 3
above) is the way to find out which of these — or something new — is
actually happening.

## Other things worth knowing

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
  it (unbranded default icon) if it's missing. Confirmed working on the
  actual ARM64 build.

- **Re-running this build** after future code changes: just repeat steps 2
  and 4 (`pyinstaller jukebox.spec` then recompile `installer.iss`) — no
  need to reinstall dependencies or re-run `fix_pywebview_windows.py`
  unless `requirements.txt` or the pywebview version changed.
