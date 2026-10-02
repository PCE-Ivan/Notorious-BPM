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


if __name__ == "__main__":
    unittest.main()
