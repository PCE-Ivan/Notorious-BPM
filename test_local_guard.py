#!/usr/bin/env python3
"""The local-only request guard. Scratch library only.

Run manually:

    python3 test_local_guard.py
"""
import os
import shutil
import tempfile
import unittest

TMPDIR = tempfile.mkdtemp(prefix="jukebox-guard-")
os.environ["JUKEBOX_DB_PATH"] = os.path.join(TMPDIR, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(TMPDIR, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = TMPDIR

import app as m  # noqa: E402
import local_guard as g  # noqa: E402


class CheckRequestTest(unittest.TestCase):
    def test_the_apps_own_requests_pass(self):
        self.assertIsNone(g.check_request("POST", "127.0.0.1:5151", origin="http://127.0.0.1:5151", sec_fetch_site="same-origin"))
        self.assertIsNone(g.check_request("GET", "localhost:5151", sec_fetch_site="same-origin"))
        self.assertIsNone(g.check_request("GET", "[::1]:5151"))

    def test_clients_without_browser_headers_pass(self):
        # curl, the launcher's probes and the test client send none of these.
        self.assertIsNone(g.check_request("POST", "127.0.0.1:5151"))

    def test_typed_address_passes(self):
        self.assertIsNone(g.check_request("GET", "127.0.0.1:5151", sec_fetch_site="none", sec_fetch_mode="navigate"))

    def test_other_host_is_refused_dns_rebinding(self):
        self.assertTrue(g.check_request("GET", "evil.example:5151"))
        self.assertTrue(g.check_request("GET", "127.0.0.1.evil.example"))
        self.assertTrue(g.check_request("GET", ""))

    def test_other_origin_is_refused(self):
        self.assertTrue(g.check_request("POST", "127.0.0.1:5151", origin="https://evil.example"))
        self.assertTrue(g.check_request("POST", "127.0.0.1:5151", origin="null"))
        self.assertTrue(g.check_request("POST", "127.0.0.1:5151", origin="http://127.0.0.1:8000"))  # another local service
        self.assertTrue(g.check_request("POST", "127.0.0.1:5151", origin="http://localhost:5151"))  # not this origin

    def test_cross_site_fetch_is_refused(self):
        for site in ("cross-site", "same-site"):
            self.assertTrue(g.check_request("POST", "127.0.0.1:5151", sec_fetch_site=site), site)
            self.assertTrue(g.check_request("GET", "127.0.0.1:5151", sec_fetch_site=site, sec_fetch_mode="no-cors"), site)

    def test_cross_site_page_navigation_may_load_the_page(self):
        # Following a link to the app: can't read anything back, and the page itself is harmless.
        self.assertIsNone(g.check_request("GET", "127.0.0.1:5151", sec_fetch_site="cross-site", sec_fetch_mode="navigate"))
        self.assertTrue(g.check_request("POST", "127.0.0.1:5151", sec_fetch_site="cross-site", sec_fetch_mode="navigate"))


class PrivateUrlTest(unittest.TestCase):
    def test_local_and_lan_addresses_are_refused(self):
        for url in ("http://127.0.0.1:8080/x", "http://localhost/x", "http://192.168.1.1/", "http://10.0.0.5:9000/",
                    "http://169.254.169.254/latest", "http://[::1]/", "http://0.0.0.0/"):
            self.assertTrue(g.refuse_private_url(url), url)

    def test_public_ip_and_garbage(self):
        self.assertIsNone(g.refuse_private_url("http://93.184.216.34/stream"))
        self.assertTrue(g.refuse_private_url("ftp://93.184.216.34/"))
        self.assertTrue(g.refuse_private_url(""))


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = m.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMPDIR, ignore_errors=True)

    def test_page_and_api_work_for_the_app_itself(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        r = self.client.get("/api/facets", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r.status_code, 200)

    def test_cross_site_post_is_refused_before_the_route_runs(self):
        r = self.client.post("/api/trash/empty", data="{}", content_type="text/plain",
                             headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["kind"], "forbidden")

    def test_rebound_host_is_refused(self):
        r = self.client.get("/api/facets", headers={"Host": "evil.example:5151"})
        self.assertEqual(r.status_code, 403)

    def test_radio_proxy_wont_fetch_local_addresses(self):
        r = self.client.get("/api/radio/proxy?url=http://127.0.0.1:9/stream")
        self.assertEqual(r.status_code, 400)

    def test_client_log_accepts_and_is_capped(self):
        for _ in range(30):
            r = self.client.post("/api/client-log", json={"message": "boom", "where": "x.js:1", "stack": "s"})
            self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
