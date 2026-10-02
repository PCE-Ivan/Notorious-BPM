#!/usr/bin/env python3
"""Every script the page loads must at least parse.

A stray fragment once sat in static/activity.js for several commits: Python
tests never load JavaScript, and the page degrades quietly when one script
fails to parse (the Activity tray and system banner just don't appear). This
parses each <script src> from index.html with the JavaScriptCore engine that
ships with macOS (no node needed) -- skipped where osascript isn't available.

Run manually:

    python3 test_frontend_syntax.py
"""
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
CHECKER = os.path.join(HERE, "tools", "check_js_syntax.js")


def page_scripts():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    names = re.findall(r'<script[^>]+src="/?static/([^"?]+)', html) or re.findall(r'<script[^>]+src="([^"?]+)', html)
    return [n.split("static/")[-1] for n in names if n.endswith(".js")]


class FrontendSyntaxTest(unittest.TestCase):
    def test_page_lists_scripts(self):
        self.assertIn("app.js", page_scripts())

    def test_every_script_exists(self):
        for name in page_scripts():
            self.assertTrue(os.path.exists(os.path.join(STATIC, name)), f"index.html loads missing script {name}")

    @unittest.skipUnless(shutil.which("osascript"), "needs macOS's JavaScriptCore via osascript")
    def test_every_script_parses(self):
        paths = [os.path.join(STATIC, n) for n in page_scripts()]
        out = subprocess.run(
            ["osascript", "-l", "JavaScript", CHECKER] + paths,
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        lines = (out.stdout + out.stderr).strip().splitlines()
        bad = [l for l in lines if not l.startswith("ok")]
        self.assertEqual(len(lines), len(paths), out.stdout + out.stderr)
        self.assertEqual(bad, [], "\n".join(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
