#!/usr/bin/env python3
"""Saving the AcoustID ("Song ID") key: tidying what was pasted, checking it
with AcoustID (mocked -- no network), and what the page is told. Scratch config only.

Run manually:

    python3 test_songid_key.py
"""
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

TMPDIR = tempfile.mkdtemp(prefix="jukebox-songid-")
os.environ["JUKEBOX_DB_PATH"] = os.path.join(TMPDIR, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(TMPDIR, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = TMPDIR

import app as m  # noqa: E402
import radio_routes as r  # noqa: E402


def acoustid_says(code=None, ok=False):
    """A fake urlopen: AcoustID answering with an error code (HTTP 400) or ok."""
    def fake(req, timeout=None):
        if ok:
            return io.BytesIO(json.dumps({"status": "ok", "results": []}).encode())
        body = json.dumps({"status": "error", "error": {"code": code, "message": "x"}}).encode()
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))
    return fake


class CleanKeyTest(unittest.TestCase):
    def test_what_gets_pasted(self):
        for pasted in ("AbCd1234Ef", "  AbCd1234Ef \n", '"AbCd1234Ef"', "API key: AbCd1234Ef", "AbCd1234Ef.", "key AbCd1234Ef"):
            self.assertEqual(r.clean_acoustid_key(pasted), "AbCd1234Ef", pasted)
        self.assertEqual(r.clean_acoustid_key("   "), "")
        self.assertEqual(r.clean_acoustid_key(None), "")


class CheckKeyTest(unittest.TestCase):
    def check(self, fake):
        with mock.patch("urllib.request.urlopen", fake):
            return r.check_acoustid_key("whatever")

    def test_states(self):
        self.assertEqual(self.check(acoustid_says(code=4)), "invalid")        # "invalid API key"
        self.assertEqual(self.check(acoustid_says(code=3)), "valid")          # it only disliked the dummy fingerprint
        self.assertEqual(self.check(acoustid_says(ok=True)), "valid")
        self.assertEqual(self.check(acoustid_says(code=14)), "unchecked")     # rate limited etc.

        def offline(req, timeout=None):
            raise urllib.error.URLError("no network")
        self.assertEqual(self.check(offline), "unchecked")


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    def saved(self):
        return m.jukebox_config.load_config().get("acoustidApiKey")

    def post(self, key, fake):
        with mock.patch("urllib.request.urlopen", fake):
            return self.client.post("/api/radio/acoustid-key", json={"acoustidApiKey": key}).get_json()

    def test_1_nothing_set_at_first(self):
        data = self.client.get("/api/radio/acoustid-key").get_json()
        self.assertFalse(data["set"])
        self.assertIn("acoustid.org", data["signup"])

    def test_2_a_rejected_key_is_not_saved(self):
        res = self.post("WrongKey99", acoustid_says(code=4))
        self.assertFalse(res["ok"])
        self.assertEqual(res["state"], "invalid")
        self.assertFalse(self.saved())

    def test_3_a_good_key_is_cleaned_checked_and_saved(self):
        res = self.post("  API key: GoodKey1234 ", acoustid_says(code=3))
        self.assertEqual((res["ok"], res["state"], res["hint"]), (True, "valid", "1234"))
        self.assertEqual(self.saved(), "GoodKey1234")
        data = self.client.get("/api/radio/acoustid-key").get_json()
        self.assertEqual((data["set"], data["hint"]), (True, "1234"))
        self.assertNotIn("GoodKey1234", json.dumps(data))   # the key itself never goes back to the page

    def test_4_offline_saves_but_says_it_was_not_checked(self):
        def offline(req, timeout=None):
            raise urllib.error.URLError("no network")
        res = self.post("OtherKey5678", offline)
        self.assertEqual((res["ok"], res["state"]), (True, "unchecked"))
        self.assertEqual(self.saved(), "OtherKey5678")

    def test_5_empty_removes_it(self):
        res = self.client.post("/api/radio/acoustid-key", json={"acoustidApiKey": ""}).get_json()
        self.assertEqual(res["state"], "removed")
        self.assertFalse(self.saved())


if __name__ == "__main__":
    unittest.main(verbosity=2)
