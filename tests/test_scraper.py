"""Offline tests for scraper.py. Run with:  python -m unittest discover -s tests -v

The end-to-end test starts a tiny local web server that imitates the parts of
ipalibrary.me the scraper relies on (sitemaps + app pages), so no internet is needed.
"""

import base64
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scraper  # noqa: E402

ALLOWED = scraper.DEFAULT_ALLOWED_HOSTS


def setUpModule():
    scraper.RETRY_BACKOFF_SECONDS = 0   # don't actually wait between retries in tests


def dl_href(target_url: str, percent_encode_padding: bool = False) -> str:
    """Build a download-button link the way the website does."""
    data = base64.b64encode(f"url={target_url}&time=15".encode()).decode()
    if percent_encode_padding:
        data = data.replace("=", "%3D")
    return f"https://ipalibrary.me/dl?data={data}"


def app_page(name, dev, version, href, modified="2024-05-06T07:08:09+00:00", icon="/i/{n}-150x150.png", desc="Great app. […]"):
    return f"""<html><head>
<meta property="article:modified_time" content="{modified}">
<meta property="og:description" content="{desc}">
<meta property="og:image" content="http://ipalibrary.me/i/{name}.png">
</head><body>
<img class="img-fluid rounded-5 lazy" src="data:image/gif;base64,R0lGOD" data-src="{icon.format(n=name)}">
<h1>{name}</h1>
<div class="ipa-developer"><i class="bi bi-person-fill"></i> {dev}</div>
<div class="ipa-developer">Version: {version}</div>
<a href="{href}">Download</a><a href="{href}">Download</a>
</body></html>"""


class UnitTests(unittest.TestCase):
    # The exact link format seen on https://ipalibrary.me/utm/
    UTM_B64 = ("dXJsPWh0dHBzOi8vYXJjaGl2ZS5vcmcvZG93bmxvYWQveW91LXR1YmUuLTE3LjQzLjEuLXJlYm9ybi52"
               "LTQuMC44L1VUTV80LjAuOS5pcGEmdGltZT0xNQ")
    UTM_URL = "https://archive.org/download/you-tube.-17.43.1.-reborn.v-4.0.8/UTM_4.0.9.ipa"

    def test_decode_handles_all_padding_styles(self):
        for suffix in ("==", "%3D%3D", ""):
            href = f"https://ipalibrary.me/dl?data={self.UTM_B64}{suffix}"
            self.assertEqual(scraper.decode_download_href(href), self.UTM_URL, suffix)

    def test_decode_keeps_percent_escapes(self):
        target = "https://archive.org/download/x/RealPads%2B%2B_6.13.0_LeTi.ipa"
        self.assertEqual(scraper.decode_download_href(dl_href(target)), target)

    def test_decode_rejects_garbage(self):
        self.assertIsNone(scraper.decode_download_href("https://ipalibrary.me/dl?data=@@@@"))
        self.assertIsNone(scraper.decode_download_href("https://ipalibrary.me/dl"))

    def test_normalize_encodes_spaces_but_not_existing_escapes(self):
        self.assertEqual(scraper.normalize_url("https://archive.org/d/My File%2B.ipa"),
                         "https://archive.org/d/My%20File%2B.ipa")

    def test_validate_accepts_and_rejects(self):
        ok = scraper.validate_download_url
        self.assertEqual(ok("https://archive.org/download/a/b.ipa", ALLOWED), ("https://archive.org/download/a/b.ipa", ""))
        self.assertEqual(ok("https://github.com/o/r/releases/download/v1/App.ipa", ALLOWED)[1], "")
        self.assertIn("host-not-allowed", ok("http://u.pc.cd/lJt7", ALLOWED)[1])
        self.assertIn("not-a-direct-ipa", ok("https://github.com/o/r/releases/download/v1/App.tipa", ALLOWED)[1])
        self.assertIn("host-not-allowed", ok("https://disk.yandex.com/d/abc", ALLOWED)[1])
        self.assertEqual(ok("http://archive.org/a.ipa", ALLOWED)[1], "not-https")
        self.assertEqual(ok("https://archive.org@evil.example/a.ipa", ALLOWED)[1], "credentials-in-url")
        self.assertIn("host-not-allowed", ok("https://notarchive.org/a.ipa", ALLOWED)[1])
        self.assertIn("host-not-allowed", ok("https://archive.org.evil.example/a.ipa", ALLOWED)[1])
        self.assertEqual(ok("https://ia800.us.archive.org/a.ipa", ALLOWED)[1], "")   # subdomain OK
        self.assertEqual(ok("http://u.pc.cd/x", ())[1], "not-https")                  # '*' = any host

    def test_clean_version_with_real_examples(self):
        c = scraper.clean_version
        self.assertEqual(c("4.0.9"), ("4.0.9", "4.0.9"))
        self.assertEqual(c("5.7.5 (5.9.0)"), ("5.7.5", "5.7.5 (5.9.0)"))
        self.assertEqual(c("9.1.0 (T6.2.2) for iOS 16.1 or later")[0], "9.1.0")
        self.assertEqual(c("19.08.2-3.0.3")[0], "19.08.2")
        self.assertEqual(c("v2.1")[0], "2.1")
        self.assertEqual(c(""), ("1.0", "1.0"))
        self.assertEqual(c("Beta"), ("Beta", "Beta"))

    def test_bundle_ids_are_unique_per_page(self):
        a = scraper.make_bundle_id("https://ipalibrary.me/stardew-valley/")
        b = scraper.make_bundle_id("https://ipalibrary.me/stardew-valley-2/")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "me.ipalibrary.stardew-valley")

    def test_sitemap_parsing_ignores_image_loc(self):
        xml = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
            <url><loc>https://ipalibrary.me/utm/</loc><lastmod>2022-11-11T11:31:01+00:00</lastmod>
            <image:image><image:loc>http://ipalibrary.me/wp-content/uploads/UTM.png</image:loc></image:image></url></urlset>"""
        entries = scraper.parse_page_sitemap(xml)
        self.assertEqual(entries, [scraper.PageEntry("https://ipalibrary.me/utm/", "2022-11-11T11:31:01+00:00")])


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802
        status, ctype, body = self.routes.get(self.path, (404, "text/plain", b"not found"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def log_message(self, *args):
        pass


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "source.json"
        _Handler.routes = {}
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def route(self, path, body, status=200, ctype="text/html; charset=utf-8"):
        _Handler.routes[path] = (status, ctype, body)

    def build_site(self, slugs_to_pages, use_index=True):
        urls = "".join(
            f"<url><loc>{self.base}/{slug}/</loc><lastmod>2023-01-01T00:00:00+00:00</lastmod></url>"
            for slug in slugs_to_pages
        )
        sitemap = (f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                   f"<url><loc>{self.base}/</loc></url>"
                   f"<url><loc>https://elsewhere.example/should-be-ignored/</loc></url>{urls}</urlset>")
        self.route("/post-sitemap.xml", sitemap, ctype="text/xml")
        if use_index:
            self.route("/sitemap_index.xml",
                       f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                       f"<sitemap><loc>{self.base}/post-sitemap.xml</loc></sitemap>"
                       f"<sitemap><loc>{self.base}/page-sitemap.xml</loc></sitemap></sitemapindex>", ctype="text/xml")
        for slug, page in slugs_to_pages.items():
            if isinstance(page, tuple):
                self.route(f"/{slug}/", page[1], status=page[0])
            else:
                self.route(f"/{slug}/", page)

    def run_scraper(self, *extra):
        return scraper.main(["--base-url", self.base, "--output", str(self.out), "--delay", "0",
                             "--min-apps", "1", "--source-url", "https://example.test/s.json", *extra])

    def test_full_run(self):
        self.build_site({
            "utm": app_page("UTM", "osy", "4.0.9", dl_href("https://archive.org/download/a/UTM_4.0.9.ipa")),
            "stardew-valley": app_page("Stardew Valley", "ConcernedApe", "1.5", dl_href("https://archive.org/download/b/Stardew%20Valley.ipa")),
            "stardew-valley-2": app_page("Stardew Valley", "ConcernedApe", "9.1.0 (T6.2.2) for iOS 16.1 or later",
                                         dl_href("https://archive.org/download/c/Sd 2.ipa", percent_encode_padding=True)),
            "short-link": app_page("Short", "x", "1.0", dl_href("http://u.pc.cd/lJt7")),
            "trollapp": app_page("Troll", "x", "1.0", dl_href("https://github.com/o/r/releases/download/v1/T.tipa")),
            "plain-post": "<html><body><h1>News</h1><p>no download here</p></body></html>",
            "gone": (404, "nope"),
            "broken-link": app_page("Broken", "x", "1.0", "https://ipalibrary.me/dl?data=%%%"),
        })
        self.assertEqual(self.run_scraper(), 0)

        data = json.loads(self.out.read_text(encoding="utf-8"))
        names = [a["name"] for a in data["apps"]]
        self.assertEqual(names, ["Stardew Valley", "Stardew Valley", "UTM"])      # sorted; junk filtered
        ids = [a["bundleIdentifier"] for a in data["apps"]]
        self.assertEqual(len(set(ids)), 3)                                           # same name, still unique IDs

        utm = next(a for a in data["apps"] if a["name"] == "UTM")
        self.assertEqual(utm["bundleIdentifier"], "me.ipalibrary.utm")
        self.assertEqual(utm["developerName"], "osy")
        self.assertEqual(utm["version"], "4.0.9")
        self.assertEqual(utm["versionDate"], "2024-05-06")                           # from the page, not "today"
        self.assertEqual(utm["versions"][0]["date"], "2024-05-06")
        self.assertEqual(utm["downloadURL"], "https://archive.org/download/a/UTM_4.0.9.ipa")
        self.assertEqual(utm["versions"][0]["downloadURL"], utm["downloadURL"])
        self.assertTrue(utm["iconURL"].endswith("/i/UTM-150x150.png"))               # data-src beats the placeholder
        self.assertIn("Great app.…", utm["localizedDescription"])
        self.assertNotIn("[…]", utm["localizedDescription"])

        messy = next(a for a in data["apps"] if a["bundleIdentifier"].endswith("stardew-valley-2"))
        self.assertEqual(messy["version"], "9.1.0")
        self.assertEqual(messy["versions"][0]["marketingVersion"], "9.1.0 (T6.2.2) for iOS 16.1 or later")
        self.assertEqual(messy["downloadURL"], "https://archive.org/download/c/Sd%202.ipa")

        self.assertEqual(data["sourceURL"], "https://example.test/s.json")
        self.assertEqual(data["apps"][0]["size"], 0)
        self.assertEqual(data["news"], [])

    def test_output_is_deterministic(self):
        self.build_site({"utm": app_page("UTM", "osy", "4.0.9", dl_href("https://archive.org/download/a/UTM.ipa"))})
        self.assertEqual(self.run_scraper(), 0)
        first = self.out.read_bytes()
        self.assertEqual(self.run_scraper(), 0)
        self.assertEqual(first, self.out.read_bytes())        # no timestamps => no pointless git commits

    def test_falls_back_when_index_missing(self):
        self.build_site({"utm": app_page("UTM", "osy", "4.0.9", dl_href("https://archive.org/download/a/UTM.ipa"))},
                        use_index=False)
        self.assertEqual(self.run_scraper(), 0)

    def test_sanity_check_protects_existing_file(self):
        pages = {f"app{i}": app_page(f"App{i}", "d", "1.0", dl_href(f"https://archive.org/download/a/{i}.ipa")) for i in range(10)}
        self.build_site(pages)
        self.assertEqual(self.run_scraper(), 0)
        good = self.out.read_bytes()

        # The site "breaks": only 3 of 10 pages work now.
        for i in range(3, 10):
            self.route(f"/app{i}/", "oops", status=500)
        self.assertEqual(self.run_scraper("--workers", "2"), 1)
        self.assertEqual(self.out.read_bytes(), good)         # old file untouched

        self.assertEqual(self.run_scraper("--force"), 0)      # explicit override still works
        self.assertEqual(len(json.loads(self.out.read_text())["apps"]), 3)

    def test_zero_apps_never_writes(self):
        self.build_site({"plain": "<html><body>nothing</body></html>"})
        self.assertEqual(scraper.main(["--base-url", self.base, "--output", str(self.out), "--delay", "0"]), 1)
        self.assertFalse(self.out.exists())

    def test_sitemap_failure_aborts(self):
        self.assertEqual(self.run_scraper(), 1)               # nothing is served -> 404 everywhere
        self.assertFalse(self.out.exists())

    def test_allow_any_host_flag(self):
        self.build_site({"x": app_page("X", "d", "1.0", dl_href("https://files.example.org/x.ipa"))})
        self.assertEqual(self.run_scraper(), 1)                                    # default list rejects it
        self.assertEqual(self.run_scraper("--allowed-hosts", "files.example.org"), 0)


if __name__ == "__main__":
    unittest.main()
