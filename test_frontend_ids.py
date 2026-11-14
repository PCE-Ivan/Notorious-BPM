#!/usr/bin/env python3
"""Every element the scripts look up by id must exist.

el("some-id") on a missing element returns null, and the first thing the page
does with it (a .addEventListener at load time, say) throws -- which stops the
rest of that script from running, so a renamed or removed id quietly kills
unrelated features. There's no browser in the test run, so this checks the
literals statically: each el("...") / getElementById("...") in the scripts
must be an id in index.html, or one the scripts create themselves
(id="..." inside a template string).

Run manually:

    python3 test_frontend_ids.py
"""
import os
import re
import unittest

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


def scripts():
    return [n for n in sorted(os.listdir(STATIC)) if n.endswith(".js")]


class FrontendIdsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = read("index.html")
        cls.html_ids = set(re.findall(r'\bid="([^"$]+)"', cls.html))
        cls.created = set()
        for name in scripts():
            src = read(name)
            cls.created.update(re.findall(r'\bid=\\?"([A-Za-z][\w-]*)\\?"', src))          # id="x" inside template strings
            cls.created.update(re.findall(r"\.id\s*=\s*[\"']([\w-]+)[\"']", src))           # el.id = "x"

    def test_index_has_no_duplicate_ids(self):
        ids = re.findall(r'\bid="([^"$]+)"', self.html)
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        self.assertEqual(dupes, [])

    def test_every_looked_up_id_exists(self):
        known = self.html_ids | self.created
        missing, looked_up = {}, set()
        for name in scripts():
            src = read(name)
            for m in re.finditer(r'\b(?:el|getElementById)\(\s*"([^"]+)"\s*\)', src):
                looked_up.add(m.group(1))
                if m.group(1) not in known:
                    missing.setdefault(m.group(1), set()).add(name)
        self.assertGreater(len(looked_up), 200, "the pattern stopped matching the scripts' el(\"...\") calls")
        self.assertEqual({k: sorted(v) for k, v in missing.items()}, {})

    def test_every_script_in_the_folder_is_loaded_by_the_page(self):
        loaded = set(re.findall(r'<script src="([^"]+\.js)"', self.html))
        self.assertEqual(set(scripts()) - loaded, set(), "a script exists in static/ but index.html never loads it")

    def test_icon_only_buttons_have_an_accessible_name(self):
        # A button whose whole content is a glyph (✕ ⏮ 📜) is announced as that glyph; the
        # tooltip isn't a reliable name. Needs an aria-label.
        unlabeled = []
        for m in re.finditer(r"<button\b([^>]*)>([^<]{1,4})</button>", self.html):
            attrs = m.group(1)
            if "aria-label" not in attrs and not re.search(r"[A-Za-z]{2}", m.group(2)):
                ident = re.search(r'id="([^"]+)"', attrs)
                unlabeled.append(ident.group(1) if ident else m.group(0)[:60])
        self.assertEqual(unlabeled, [])

    def test_every_icon_glyph_has_a_drawing(self):
        src = read("icons.js")
        paths = set(re.findall(r"^  (\w+): '", src, re.M))
        mapped = set(re.findall(r'"[^"]+": "(\w+)"', src.split("const ICON_FOR")[1].split("};")[0]))
        self.assertGreater(len(mapped), 30)
        self.assertEqual(mapped - paths, set())

    def test_icons_load_before_everything_else(self):
        order = re.findall(r'<script src="([^"]+\.js)"', self.html)
        self.assertEqual(order[0], "icons.js")   # app.js builds rows with ICON_SEARCH_HTML
        self.assertEqual(order[-1], "shell.js")  # shell.js wraps what the others set up

if __name__ == "__main__":
    unittest.main(verbosity=2)
