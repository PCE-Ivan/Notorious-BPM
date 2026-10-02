#!/usr/bin/env python3
"""Tests for desktop_macos.py's choice of which server a window attaches to.
Uses throwaway local HTTP servers; never starts the real app or a window.

Run manually:

    python3 test_desktop_launcher.py
"""
import http.server
import json
import os
import socket
import tempfile
import threading
import unittest

os.environ.setdefault("JUKEBOX_CONFIG_PATH", os.path.join(tempfile.gettempdir(), "jukebox-launchertest-config.json"))
import desktop_macos  # noqa: E402  (imports only stdlib + config; webview is imported lazily inside main)


def _start_server(payload=None, status=200):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class ChoosePortTest(unittest.TestCase):
    def test_free_preferred_port_is_used_and_not_attached(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertEqual(desktop_macos._choose_port(port, "/lib/a.nbpmlib"), (port, False))

    def test_same_app_same_library_is_joined(self):
        server, port = _start_server({"app": "notorious-bpm", "library": "/lib/a.nbpmlib"})
        try:
            self.assertEqual(desktop_macos._choose_port(port, "/lib/a.nbpmlib"), (port, True))
        finally:
            server.shutdown()

    def test_same_app_different_library_is_not_joined(self):
        server, port = _start_server({"app": "notorious-bpm", "library": "/lib/other.nbpmlib"})
        try:
            chosen, attach = desktop_macos._choose_port(port, "/lib/a.nbpmlib")
            self.assertFalse(attach)
            self.assertNotEqual(chosen, port)
        finally:
            server.shutdown()

    def test_something_else_entirely_on_the_port_is_not_joined(self):
        server, port = _start_server({"hello": "world"})
        try:
            chosen, attach = desktop_macos._choose_port(port, "/lib/a.nbpmlib")
            self.assertFalse(attach)
            self.assertNotEqual(chosen, port)
        finally:
            server.shutdown()

    def test_older_build_without_the_endpoint_is_not_joined(self):
        server, port = _start_server(None)  # 404 on /api/instance
        try:
            self.assertFalse(desktop_macos._choose_port(port, "/lib/a.nbpmlib")[1])
        finally:
            server.shutdown()

    def test_instance_info_ignores_non_matching_servers(self):
        server, port = _start_server({"app": "something-else"})
        try:
            self.assertIsNone(desktop_macos._instance_info(port))
        finally:
            server.shutdown()


class DropTest(unittest.TestCase):
    def test_paths_come_from_pywebviews_full_path_field(self):
        event = {"type": "drop", "dataTransfer": {"files": [
            {"name": "a.mp3", "pywebviewFullPath": "/Users/me/a.mp3"},
            {"name": "no-path.mp3"},                                  # pywebview couldn't resolve it
            {"name": "Album", "pywebviewFullPath": "/Users/me/Album"},
        ]}}
        self.assertEqual(desktop_macos._dropped_paths(event), ["/Users/me/a.mp3", "/Users/me/Album"])

    def test_malformed_events_yield_nothing(self):
        for event in (None, {}, {"dataTransfer": None}, {"dataTransfer": {"files": None}}, {"dataTransfer": {"files": ["str"]}}):
            self.assertEqual(desktop_macos._dropped_paths(event), [])

    def test_paths_are_posted_to_the_import_route(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = b'{"started": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = desktop_macos._send_to_import(server.server_address[1], ["/a.mp3", "/b folder"])
        finally:
            server.shutdown()
        self.assertEqual(result, {"started": True})
        self.assertEqual(seen["path"], "/api/import/files")
        self.assertEqual(seen["body"], {"paths": ["/a.mp3", "/b folder"]})


class MenuTest(unittest.TestCase):
    class FakeWindow:
        def __init__(self):
            self.js = []

        def evaluate_js(self, code):
            self.js.append(code)

    @staticmethod
    def walk(items):
        from webview.menu import Menu
        for item in items:
            if isinstance(item, Menu):
                yield from MenuTest.walk(item.items)
            else:
                yield item

    def test_every_action_drives_something_the_page_really_has(self):
        import re
        window = self.FakeWindow()
        menus = desktop_macos._build_menu(window)
        self.assertEqual([m.title for m in menus], ["File", "Browse", "Playback", "Tools"])
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"), encoding="utf-8") as f:
            html = f.read()
        actions = [a for a in self.walk(menus) if hasattr(a, "function")]
        self.assertGreater(len(actions), 25)
        for action in actions:
            window.js.clear()
            action.function()
            self.assertEqual(len(window.js), 1, action.title)
            m = re.search(r'getElementById\("([^"]+)"\)', window.js[0])
            if m and "click()" in window.js[0]:
                self.assertIn(f'id="{m.group(1)}"', html, f"menu item {action.title!r} clicks a button that doesn't exist")

    def test_functions_called_from_the_menu_exist_in_the_page_scripts(self):
        import re
        window = self.FakeWindow()
        static = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        source = "".join(open(os.path.join(static, n), encoding="utf-8").read() for n in os.listdir(static) if n.endswith(".js"))
        called = set()
        for action in [a for a in self.walk(desktop_macos._build_menu(window)) if hasattr(a, "function")]:
            window.js.clear()
            action.function()
            called.update(re.findall(r"\b(setBrowseView|applyLayoutMode|applyTheme|togglePlayPause|playNext|playPrevious)\(", window.js[0]))
        self.assertEqual(called, {"setBrowseView", "applyLayoutMode", "applyTheme", "togglePlayPause", "playNext", "playPrevious"})
        for name in called:
            self.assertRegex(source, rf"function {name}\(", f"{name} isn't defined by any page script")


if __name__ == "__main__":
    unittest.main()
