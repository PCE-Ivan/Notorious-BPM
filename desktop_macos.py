#!/usr/bin/env python3
"""Experimental standalone-window entry point for Notorious B.P.M. on macOS.

A prototype, separate from the existing shell launcher (which opens the app
in the default browser): starts the same Flask backend on a background
thread, then shows it in a native pywebview window instead of a browser
tab -- own title bar, own Dock icon, no address bar or other tabs.

Needs `pip install --user pywebview pyobjc-framework-Cocoa
pyobjc-framework-WebKit` first (pywebview's macOS backend is Cocoa, which
pulls in pyobjc -- a compiled dependency the existing launcher deliberately
avoids, which is exactly why this lives as a separate opt-in script rather
than replacing it outright).

Not yet wired into the packaged .app or the production launcher -- run it
directly to try it out:

    python3 desktop_macos.py
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

import config as jukebox_config

PORT = int(os.environ.get("JUKEBOX_PORT", "5151"))  # preferred port
ACTIVE_PORT = PORT  # the port this launch actually ended up on -- see main()


def _instance_info(port):
    """What's answering on `port`, as /api/instance reports it -- or None if
    nothing is, or it isn't this app (or is an older build without the
    endpoint). Deliberately not /api/facets: that touches the library, so a
    perfectly healthy server whose library is momentarily unreadable would
    look "down" here."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/instance", timeout=1) as resp:
            info = json.load(resp)
        return info if isinstance(info, dict) and info.get("app") == "notorious-bpm" else None
    except Exception:
        return None


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _free_port(after):
    for candidate in range(after + 1, after + 60):
        if not _port_in_use(candidate):
            return candidate
    raise RuntimeError("No free local port found")


def _bundled_base_dir():
    # When frozen by PyInstaller (see jukebox_macos.spec), the bundled
    # "static" folder lives under sys._MEIPASS, not next to this script --
    # same reasoning as launcher.py's identical helper. Previously this
    # script was always launched by a shell wrapper that set
    # JUKEBOX_STATIC_DIR itself before exec'ing python; PyInstaller replaces
    # that wrapper entirely, so this script needs to resolve it itself now.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _prompt_for_music_folder():
    """A first launch with no music folder configured used to print to
    stderr and quit -- invisible from a double-clicked .app, since nothing
    ever opens a Terminal to show that message (JB-004). This mirrors the
    picker Jukebox.app's shell launcher already shows on its own first run,
    so a fresh install of either build ends in a working library instead of
    the app silently doing nothing. Returns the picked path, or None if the
    user cancelled."""
    result = subprocess.run(
        ["osascript", "-e",
         'POSIX path of (choose folder with prompt "Select your music folder for Notorious B.P.M.")'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None  # covers both "user cancelled" and any osascript error
    return result.stdout.strip().rstrip("/") or None


def _auto_backup_on_close():
    """Silently exports ratings + playlists to a rolling backup file every
    time the window closes -- so "started from nothing after switching
    folders" (or any other DB wipe) has a same-day fallback to restore from
    via the existing Import feature, without needing to remember to click
    Backup yourself first. Hits the running server's own /api/export over
    HTTP rather than importing app.py directly, since this needs to work
    whether THIS process is the one that started the server or another
    launch of the app already had it running -- either way it's the same
    DB file underneath. Keeps the last 5, same rotation as _snapshot_db()
    in app.py. Never blocks the window from actually closing: any failure
    here (server already gone, disk full, ...) is logged and swallowed."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{ACTIVE_PORT}/api/export", timeout=5) as resp:
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
        urllib.request.urlopen(f"http://127.0.0.1:{ACTIVE_PORT}/api/radio/levels/stop-all", data=b"", timeout=3)
    except Exception:
        pass


FOCUS_SIZE = (360, 640)  # fallback, used only if the page can't report a panel size
FOCUS_MIN = (300, 400)
FOCUS_MAX = (700, 900)


class JsApi:
    """Exposed to the page as window.pywebview.api -- lets app.js's focus
    mode shrink the actual OS window down to a small standalone-looking
    player instead of just rearranging content within the full-size window
    (which is all a browser tab can do). Only meaningful under pywebview;
    app.js falls back to its existing CSS-only behavior when this isn't
    present, e.g. when running in a regular browser tab. `window` is filled
    in right after create_window() returns it -- the window doesn't exist
    yet at the point create_window() itself needs this object.

    enter_focus is passed the theme panel's own natural (unscaled) size --
    resizing the OS window to that exact aspect ratio is what lets app.js's
    scale-to-fit math land on the same factor for both width and height, so
    the panel fills the window completely edge to edge instead of leaving
    letterbox margins on whichever axis doesn't match."""

    def __init__(self):
        # Underscore-prefixed deliberately: pywebview's JS-API exposure
        # walker (inject_pywebview -> get_functions in its util.py) uses
        # dir(self._js_api) to auto-discover what to expose to the page,
        # skipping anything whose name starts with "_" but otherwise
        # recursing into any non-callable attribute. A plain "self.window"
        # here gets walked straight into the native OS window object,
        # which on Windows recurses infinitely into .NET's Rectangle.Empty
        # and freezes the app (JB-011) -- kept underscore-prefixed here
        # too so this class stays identical across all three platform
        # launchers rather than only fixing it where it was caught.
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
            # Scale the whole panel uniformly to land inside [MIN, MAX] on
            # both axes -- clamping each axis independently would distort
            # the aspect ratio and reintroduce the letterboxing this exists
            # to avoid.
            scale = max(1.0, FOCUS_MIN[0] / w, FOCUS_MIN[1] / h)
            scale = min(scale, FOCUS_MAX[0] / w, FOCUS_MAX[1] / h)
            target = (round(w * scale), round(h * scale))
        self._window.resize(*target)

    def exit_focus(self):
        if not self._window or self._normal_size is None:
            return
        self._window.resize(*self._normal_size)

    def save_export(self, content):
        """Backs the "Backup" button's actual file save. A plain
        Blob+<a download> (what the button used to do unconditionally)
        silently does nothing in pywebview's WKWebView -- it doesn't wire up
        the delegate methods a real download needs, so the click just
        looked broken with no error at all. Native Save panel instead,
        exactly like _prompt_for_music_folder's Open panel above."""
        result = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose file name with prompt '
             '"Save Notorious B.P.M. backup as:" default name "notorious-bpm-backup.json")'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return {"ok": False, "cancelled": True}
        path = result.stdout.strip()
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"ok": True, "path": path}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def open_external_url(self, url):
        """A plain <a href target="_blank"> is unreliable inside WKWebView
        (it can silently do nothing, or navigate the app's own window away
        from itself instead of opening a real browser) -- used for the
        About panel's donate link. Restricted to http(s) so this bridge
        method can't be repurposed to open arbitrary local files/schemes."""
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            return {"ok": False, "error": "Refused non-http(s) URL"}
        webbrowser.open(url)
        return {"ok": True}


def _touch_protected_locations(jukebox_app):
    """Reads the library file and music folder once, here on the main thread
    before any window exists. macOS raises its "allow access to your
    Desktop / external drive?" prompt at the first read of a protected
    location -- doing that first read from the server's worker threads (as
    the first library query otherwise would) is how the prompt got missed
    and the access silently denied. Failure is fine and expected when
    access isn't granted yet: the UI's own status check explains it."""
    import logging
    log = logging.getLogger("jukebox.startup")
    for path, is_dir in ((jukebox_app.DB_PATH, False), (jukebox_app.MUSIC_DIR, True)):
        if not path:
            continue
        try:
            if is_dir:
                os.listdir(path)
            else:
                with open(path, "rb") as f:
                    f.read(16)
        except OSError as e:
            log.warning("Startup access probe could not read %s: %s", path, e)


def _choose_port(preferred, db_path):
    """(port, attach). Is something already serving on the preferred port --
    and is it *this* library? Joining a running server is right when it's
    this app on this library (a second launch just opens another window on
    it). It's wrong for anything else: a window silently attached to a
    different library's server (a dev instance, a test server, another copy
    of the app) would show and act on the wrong data with nothing to say
    so. In that case a free port is used and we start our own server."""
    if not _port_in_use(preferred):
        return preferred, False
    info = _instance_info(preferred)
    if info and os.path.normpath(info.get("library") or "") == os.path.normpath(db_path or ""):
        return preferred, True
    return _free_port(preferred), False


# ---------------------------------------------------------- drag and drop --
# Files dropped on the window. A web page never learns the real path of a file
# dropped on it, but pywebview's DOM events do (it adds "pywebviewFullPath" to
# each file), so the drop is caught here and handed to the local server's
# import route -- the page itself only shows the "drop here" overlay and, via
# the Activity tray, the progress.
def _dropped_paths(event):
    files = ((event or {}).get("dataTransfer") or {}).get("files") or []
    return [f["pywebviewFullPath"] for f in files if isinstance(f, dict) and f.get("pywebviewFullPath")]


def _send_to_import(port, paths):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/import/files",
        data=json.dumps({"paths": paths}).encode(), headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode() or "{}")


def _bind_file_drop(window):
    from webview.dom import DOMEventHandler

    def on_drop(event):
        paths = _dropped_paths(event)
        if not paths:
            return
        try:
            _send_to_import(ACTIVE_PORT, paths)
        except Exception as e:
            print(f"Couldn't hand the dropped files to the app: {e}", file=sys.stderr)

    doc = window.dom.document
    # preventDefault on all four, or WKWebView navigates to the dropped file
    # instead of firing "drop". The page keeps receiving the events too (no
    # stopPropagation), which is what lets it show its overlay.
    for name in ("dragenter", "dragstart", "dragover"):
        getattr(doc.events, name).__iadd__(DOMEventHandler(lambda e: None, True, False))   # in-place: registers on the element
    doc.events.drop += DOMEventHandler(on_drop, True, False)


def main():
    global ACTIVE_PORT
    import library_manager
    db_path, _music = library_manager.resolve_startup()

    port, attach = _choose_port(PORT, db_path)
    if port != PORT:
        print(f"Port {PORT} is taken by something else; using {port} instead.", file=sys.stderr)
    ACTIVE_PORT = port

    if not attach:
        music_dir = jukebox_config.get_music_dir()
        if not music_dir or not os.path.isdir(music_dir):
            picked = _prompt_for_music_folder()
            if not picked or not os.path.isdir(picked):
                return  # cancelled -- quit quietly, same as Jukebox.app's launcher on cancel
            jukebox_config.update_config(lambda cfg: cfg.__setitem__("music_dir", picked))
            music_dir = picked

        os.environ.setdefault("JUKEBOX_STATIC_DIR", os.path.join(_bundled_base_dir(), "static"))
        import app as jukebox_app
        _touch_protected_locations(jukebox_app)
        server_thread = threading.Thread(
            target=lambda: jukebox_app.app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False),
            daemon=True,
        )
        server_thread.start()

        for _ in range(60):
            if _instance_info(port):
                break
            time.sleep(0.5)
        else:
            print("Server didn't come up in time.", file=sys.stderr)
            return

    import webview
    api = JsApi()
    window = webview.create_window(
        "Notorious B.P.M.", f"http://127.0.0.1:{port}", width=1200, height=800, min_size=(800, 500),
        maximized=True, js_api=api,
    )
    api._window = window
    window.events.closing += _auto_backup_on_close
    def bind_drop():
        # On every page load, not once: pywebview forgets its DOM handlers
        # when the page reloads (and the listeners it injected die with the
        # old page).
        try:
            _bind_file_drop(window)
        except Exception as e:
            print(f"Drag and drop unavailable: {e}", file=sys.stderr)
    window.events.loaded += bind_drop
    # private_mode=False: pywebview defaults to a private/incognito-style
    # WKWebView, which doesn't persist storage across launches -- this was
    # the actual root cause of the theme resetting on relaunch (the
    # server-side theme save is worth keeping regardless, since it's also
    # what keeps the browser tab and this window in sync with each other).
    # debug=True was a temporary aid for diagnosing that and the tonearm
    # geometry via the inspector -- left on, it opens the Web Inspector
    # automatically on every launch, so it's off again now that both are
    # fixed.
    webview.start(private_mode=False)


if __name__ == "__main__":
    main()
