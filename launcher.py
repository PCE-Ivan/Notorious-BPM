#!/usr/bin/env python3
"""Cross-platform desktop entry point for Notorious B.P.M.

This is what gets bundled into a single executable (PyInstaller) for a real,
no-terminal, double-click app experience: resolves the music folder
(prompting once if it isn't set yet), runs the first-time library scan if
needed, starts the local Flask server, and shows it in a native pywebview
window -- own title bar and taskbar entry, no browser chrome. (Windows'
pywebview backend is EdgeChromium, using the WebView2 runtime that ships
with Windows 10/11 by default -- no extra install needed on a normal
machine.) If pywebview can't initialize for any reason (WebView2 missing on
an older/stripped-down Windows install, say), this falls back to opening the
default browser instead of crashing outright -- a rougher experience (see
BUILD_WINDOWS.md's "no system tray/Quit yet" note) but still a working one.

Unlike the macOS .app's shell launcher (which detaches the server as a
separate background process), this keeps the server in the same process on
a background thread. With the native window, that thread dies with the
process when the window closes -- closing the window quits Jukebox
cleanly, the same as any other native app, with no leftover background
process to hunt down in Task Manager. (The browser-tab fallback path can't
offer that -- there's no window to close -- so it keeps the old
run-until-killed behavior there instead.) Re-launching while a server is
already running just reuses it and opens a new window/tab against it,
rather than starting a second copy.
"""
import os
import sys
import time
import threading
import webbrowser
import urllib.parse
import urllib.request

import config as jukebox_config

PORT = int(os.environ.get("JUKEBOX_PORT", "5151"))


def _server_already_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/facets", timeout=1)
        return True
    except Exception:
        return False


def _show_info(message):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo("Notorious B.P.M.", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def _show_error(message):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror("Notorious B.P.M.", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def _pick_music_folder():
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    picked = filedialog.askdirectory(title="Select your music folder for Notorious B.P.M.")
    root.destroy()
    return picked or None


def _resolve_music_dir():
    music_dir = jukebox_config.get_music_dir()
    if music_dir and os.path.isdir(music_dir):
        return music_dir
    _show_info("Select the music folder for Notorious B.P.M. to scan.")
    picked = _pick_music_folder()
    if not picked:
        return None
    # update_config() (lock + read-modify-write + atomic write), not a bare
    # load+mutate+save -- the same data-loss bug already fixed on the macOS
    # side (JB-003): a plain overwrite here could blow away theme/
    # woodFinish/anything else already saved.
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("music_dir", picked))
    return picked


def _bundled_base_dir():
    # When frozen by PyInstaller, bundled data (the "static" folder) lives
    # under sys._MEIPASS, not next to this script.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


FOCUS_SIZE = (360, 640)  # fallback, used only if the page can't report a panel size
FOCUS_MIN = (300, 400)
FOCUS_MAX = (700, 900)


class JsApi:
    """Exposed to the page as window.pywebview.api -- lets app.js's focus
    mode shrink the actual OS window down to a small standalone-looking
    player instead of just rearranging content within the full-size window
    (which is all a browser tab can do). `window` is filled in right after
    create_window() returns it -- the window doesn't exist yet at the point
    create_window() itself needs this object.

    enter_focus is passed the theme panel's own natural (unscaled) size --
    resizing the OS window to that exact aspect ratio is what lets app.js's
    scale-to-fit math land on the same factor for both width and height, so
    the panel fills the window completely edge to edge instead of leaving
    letterbox margins on whichever axis doesn't match. Identical to the
    macOS build's desktop_macos.py -- same app.js, same bridge contract."""

    def __init__(self):
        # Underscore-prefixed deliberately: pywebview's JS-API exposure
        # walker (inject_pywebview -> get_functions in its util.py) uses
        # dir(self._js_api) to auto-discover what to expose to the page,
        # skipping anything whose name starts with "_" but otherwise
        # recursing into any non-callable attribute. A plain "self.window"
        # here gets walked straight into the native OS window object --
        # on Windows specifically, that recurses into .NET's
        # Rectangle.Empty (a value type pythonnet re-boxes as a new
        # object on every access), and the walker's id()-based
        # already-visited check can't catch that, so it recurses forever
        # and freezes the app (JB-011).
        self._window = None
        self._normal_size = None

    def enter_focus(self, natural_size=None):
        if not self._window:
            return
        if self._normal_size is None:
            self._normal_size = (self._window.width, self._window.height)
        target = FOCUS_SIZE
        if natural_size and natural_size.get("width") and natural_size.get("height"):
            w, h = natural_size["width"], natural_size["height"]
            scale = max(1.0, FOCUS_MIN[0] / w, FOCUS_MIN[1] / h)
            scale = min(scale, FOCUS_MAX[0] / w, FOCUS_MAX[1] / h)
            target = (round(w * scale), round(h * scale))
        self._window.resize(*target)

    def exit_focus(self):
        if not self._window or self._normal_size is None:
            return
        self._window.resize(*self._normal_size)

    def save_export(self, content):
        """Backs the "Backup" button's actual file save -- same reasoning
        as the macOS build's identical method: a plain Blob+<a download>
        (what the button used to do unconditionally) isn't reliable inside
        pywebview's webview, so this goes through a real native Save
        dialog instead. Untested on real Windows, like the rest of this
        build (see BUILD_WINDOWS.md)."""
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.asksaveasfilename(
                title="Save Notorious B.P.M. backup as",
                defaultextension=".json",
                initialfile="notorious-bpm-backup.json",
                filetypes=[("JSON", "*.json")],
            )
        finally:
            root.destroy()
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"ok": True, "path": path}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def open_external_url(self, url):
        """A plain <a href target="_blank"> is unreliable inside pywebview's
        native window -- used for the About panel's donate link. Restricted
        to http(s) so this bridge method can't be repurposed to open
        arbitrary local files/schemes."""
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            return {"ok": False, "error": "Refused non-http(s) URL"}
        webbrowser.open(url)
        return {"ok": True}


def _auto_backup_on_close():
    """Silently exports ratings + playlists to a rolling backup file every
    time the window closes -- same reasoning as the macOS build's identical
    function. Keeps the last 5. Never blocks the window from actually
    closing: any failure here is logged and swallowed."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/export", timeout=5) as resp:
            content = resp.read()
        backup_dir = os.path.join(jukebox_config.get_app_data_dir(), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        with open(os.path.join(backup_dir, f"export-auto-{ts}.json"), "wb") as f:
            f.write(content)
        autos = sorted(f for f in os.listdir(backup_dir) if f.startswith("export-auto-") and f.endswith(".json"))
        for old in autos[:-5]:
            os.remove(os.path.join(backup_dir, old))
    except Exception as e:
        print(f"Auto-backup on exit failed (non-fatal): {e}", file=sys.stderr)

    # A normal quit while internet radio is playing would otherwise leave
    # its ffmpeg-based VU/spectrum decode running until its own 15s idle
    # timeout catches it -- harmless, but no reason to wait for that here.
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/radio/levels/stop-all", data=b"", timeout=3)
    except Exception:
        pass


def _init_dotnet_runtime():
    """pywebview's Windows backend hosts WebView2 inside a WinForms window,
    which needs System.Windows.Forms -- part of the "WindowsDesktop"
    shared framework, not the bare "NETCore" one. pythonnet's default,
    implicit CoreCLR load (triggered by pywebview's own `import clr`) only
    asks for the bare NETCore framework, so System.Windows.Forms fails to
    load with a "could not find file or assembly" error and pywebview
    silently falls back to a plain browser tab -- explicitly loading
    CoreCLR ourselves first, with a runtime config that asks for
    WindowsDesktop specifically, is what fixes it. Needs the .NET
    Desktop Runtime installed on the machine (a normal, common thing to
    already have -- Visual Studio, many games/apps pull it in -- but not
    guaranteed on a bare-bones install)."""
    from pythonnet import load
    config_path = os.path.join(_bundled_base_dir(), "runtimeconfig.json")
    load("coreclr", runtime_config=config_path)
    # pywebview's winforms.py only pre-loads System.Windows.Forms itself
    # before doing `from Microsoft.Win32 import SystemEvents` -- under
    # classic .NET Framework (what pywebview was written against),
    # SystemEvents lived inside System.Windows.Forms's own assembly, so
    # that was enough. Under modern .NET (what WindowsDesktop.App/CoreCLR
    # actually ships), it was split into its own separate assembly, so it
    # needs its own explicit reference or that import fails.
    import clr
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("Microsoft.Win32.SystemEvents")


def _open_window():
    """Native pywebview window when possible; falls back to a plain browser
    tab if pywebview can't initialize (e.g. WebView2 runtime missing)."""
    try:
        if sys.platform == "win32":
            _init_dotnet_runtime()
        import webview
        api = JsApi()
        window = webview.create_window(
            "Notorious B.P.M.", f"http://127.0.0.1:{PORT}", width=1200, height=800, min_size=(800, 500),
            maximized=True, js_api=api,
        )
        api._window = window
        window.events.closing += _auto_backup_on_close
        # private_mode=False: without it, pywebview uses a private/
        # incognito-style browsing context that doesn't persist storage
        # across launches -- the same fix applied on the macOS build.
        webview.start(private_mode=False)
        return True
    except Exception as e:
        print(f"Native window unavailable ({e}); falling back to browser tab.", file=sys.stderr)
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        return False


def main():
    if _server_already_up():
        _open_window()
        return

    music_dir = _resolve_music_dir()
    if not music_dir:
        return  # user cancelled the folder picker on first run

    os.environ["JUKEBOX_MUSIC_DIR"] = music_dir
    data_dir = jukebox_config.get_app_data_dir()
    db_path = os.path.join(data_dir, "library.db")
    os.environ.setdefault("JUKEBOX_DB_PATH", db_path)
    os.environ.setdefault("JUKEBOX_STATIC_DIR", os.path.join(_bundled_base_dir(), "static"))

    if not os.path.isfile(db_path):
        _show_info("Indexing your music library — this happens once and may take a few minutes.")
        import scan_library
        try:
            scan_library.scan()
        except RuntimeError as e:
            _show_error(str(e))
            return

    import app as jukebox_app
    server_thread = threading.Thread(
        target=lambda: jukebox_app.app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    for _ in range(60):
        if _server_already_up():
            break
        time.sleep(0.5)

    got_native_window = _open_window()
    if not got_native_window:
        # No window to close as a quit signal in this fallback path -- keep
        # the process alive the way the original browser-tab build always
        # did. Quit Jukebox from Task Manager (or add a system tray icon
        # here later for a proper Quit option).
        server_thread.join()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _show_error(f"Notorious B.P.M. couldn't start: {e}")
        raise
