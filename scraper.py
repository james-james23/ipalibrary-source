#!/usr/bin/env python3
"""
Build an AltStore-style "source" (a JSON catalogue) from https://ipalibrary.me.

The resulting file can be added as a source in SideStore, AltStore and
LiveContainer so the apps listed on the website show up inside those apps.

How it works
------------
1. Ask the website's sitemap which pages exist (every app has its own page).
2. Download each app page and read the name, developer, version, icon,
   last-updated date and the download button.
3. The download button points to ``/dl?data=<base64>``; the base64 text hides
   the real .ipa address, so we decode it.
4. Throw away anything that is not a direct, HTTPS .ipa link on a trusted host.
5. Write everything to one JSON file, but only if the result looks sane
   (so a broken run can never replace a good file with a bad one).

Usage
-----
    python scraper.py                      # writes ipalibrary_source.json next to this file
    python scraper.py --output out.json --source-url https://example.com/source.json
    python scraper.py --help
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------- #
# Configuration (everything you might want to tweak lives here)
# --------------------------------------------------------------------------- #

DEFAULT_BASE_URL = "https://ipalibrary.me"
SITEMAP_INDEX_PATH = "/sitemap_index.xml"       # lists every sitemap (Yoast SEO plugin)
FALLBACK_SITEMAP_PATH = "/post-sitemap.xml"     # used if the index can't be read

DEFAULT_OUTPUT = Path(__file__).resolve().with_name("ipalibrary_source.json")

SOURCE_NAME = "IPALibrary Source"
SOURCE_IDENTIFIER = "me.ipalibrary.source"
BUNDLE_ID_PREFIX = "me.ipalibrary."
TINT_COLOR = "#0A84FF"

# Only download links on these hosts (or their subdomains) are kept.
# Anything else is skipped and shown in the run summary so you can decide
# whether to trust it. Override with --allowed-hosts or ALLOWED_DOWNLOAD_HOSTS.
DEFAULT_ALLOWED_HOSTS = ("archive.org", "github.com", "githubusercontent.com")

DEFAULT_WORKERS = 6             # parallel page downloads; be gentle with the site
REQUEST_DELAY_SECONDS = 0.2     # pause after each request, per worker
CONNECT_TIMEOUT = 10            # seconds
READ_TIMEOUT = 30               # seconds
RETRY_ATTEMPTS = 4              # retries on timeouts / 429 / 5xx errors
RETRY_BACKOFF_SECONDS = 1.0     # waits 1s, 2s, 4s, 8s between retries

# Safety checks before the output file is replaced.
MIN_APPS = 100                  # fewer apps than this is treated as a failed run
MIN_RATIO_VS_PREVIOUS = 0.8     # losing more than 20% of apps vs. last run is treated as a failed run

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

log = logging.getLogger("ipalibrary")


class ScraperError(RuntimeError):
    """A problem serious enough that the output file must not be updated."""


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Settings:
    base_url: str = DEFAULT_BASE_URL
    workers: int = DEFAULT_WORKERS
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS   # empty tuple = allow any host
    request_delay: float = REQUEST_DELAY_SECONDS


@dataclass(frozen=True)
class PageEntry:
    """One URL from the sitemap."""
    url: str
    lastmod: str | None  # ISO timestamp from the sitemap, used as a date fallback


@dataclass(frozen=True)
class AppRecord:
    """Everything we learned about one app page."""
    page_url: str
    name: str
    bundle_id: str
    developer: str
    version: str              # cleaned, e.g. "9.1.0"
    marketing_version: str    # as shown on the site, e.g. "9.1.0 (T6.2.2) for iOS 16.1 or later"
    release_date: str         # YYYY-MM-DD
    download_url: str
    icon_url: str
    description: str

    def to_source_entry(self) -> dict:
        """Convert to one entry of the "apps" list.

        Both the modern format (``versions`` list) and the legacy top-level
        fields (``version``, ``versionDate``, ``downloadURL``, ``size``) are
        written so old and new clients can all read the file.
        """
        version_entry = {
            "version": self.version,
            "buildVersion": self.version,
            "date": self.release_date,
            "localizedDescription": f"Version {self.marketing_version}",
            "downloadURL": self.download_url,
            "size": 0,  # unknown: IPAs are hosted elsewhere and we don't download them
        }
        if self.marketing_version != self.version:
            version_entry["marketingVersion"] = self.marketing_version

        return {
            "name": self.name,
            "bundleIdentifier": self.bundle_id,
            "developerName": self.developer,
            "localizedDescription": self.description,
            "iconURL": self.icon_url,
            "tintColor": TINT_COLOR,
            "versions": [version_entry],
            # --- legacy fields, kept for older clients ---
            "version": self.version,
            "versionDate": self.release_date,
            "downloadURL": self.download_url,
            "size": 0,
        }


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #

_thread_local = threading.local()


def _session() -> requests.Session:
    """One requests.Session per thread (sessions are not guaranteed thread-safe)."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        retry = Retry(
            total=RETRY_ATTEMPTS,
            backoff_factor=RETRY_BACKOFF_SECONDS,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=2)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _thread_local.session = session
    return session


def fetch(url: str, delay: float) -> requests.Response:
    """GET a URL with timeouts and automatic retries, then pause politely."""
    try:
        return _session().get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    finally:
        if delay:
            time.sleep(delay)


# --------------------------------------------------------------------------- #
# Step 1: find the app pages via the sitemap
# --------------------------------------------------------------------------- #

def _same_host(url: str, base_url: str) -> bool:
    return urlsplit(url).netloc.lower() == urlsplit(base_url).netloc.lower()


def _is_post_sitemap(url: str) -> bool:
    """Matches post-sitemap.xml, post-sitemap2.xml, ... (Yoast splits big sitemaps)."""
    return re.fullmatch(r"post-sitemap\d*\.xml", Path(urlsplit(url).path).name) is not None


def parse_sitemap_index(xml: bytes) -> list[str]:
    soup = BeautifulSoup(xml, "lxml-xml")
    return [
        loc.get_text(strip=True)
        for sm in soup.find_all("sitemap")
        if (loc := sm.find("loc", recursive=False))
    ]


def parse_page_sitemap(xml: bytes) -> list[PageEntry]:
    soup = BeautifulSoup(xml, "lxml-xml")
    entries = []
    for url_tag in soup.find_all("url"):
        loc = url_tag.find("loc", recursive=False)  # recursive=False skips <image:loc>
        if not loc:
            continue
        lastmod = url_tag.find("lastmod", recursive=False)
        entries.append(PageEntry(loc.get_text(strip=True), lastmod.get_text(strip=True) if lastmod else None))
    return entries


def discover_sitemaps(settings: Settings) -> list[str]:
    """Return the post sitemaps to read, falling back to the well-known one."""
    index_url = settings.base_url + SITEMAP_INDEX_PATH
    found: list[str] = []
    try:
        response = fetch(index_url, settings.request_delay)
        response.raise_for_status()
        found = [u for u in parse_sitemap_index(response.content)
                 if _is_post_sitemap(u) and _same_host(u, settings.base_url)]
    except requests.RequestException as exc:
        log.warning("Could not read %s (%s).", index_url, exc)

    if not found:
        fallback = settings.base_url + FALLBACK_SITEMAP_PATH
        log.warning("No post sitemaps found in the index; falling back to %s", fallback)
        found = [fallback]
    return found


def discover_app_pages(settings: Settings) -> list[PageEntry]:
    """Collect every candidate app page URL. Any sitemap failure aborts the run,
    because a partial list would silently drop apps."""
    base = urlsplit(settings.base_url)
    pages: dict[str, PageEntry] = {}

    for sitemap_url in discover_sitemaps(settings):
        log.info("Reading %s", sitemap_url)
        try:
            response = fetch(sitemap_url, settings.request_delay)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ScraperError(f"Could not read sitemap {sitemap_url}: {exc}") from exc

        for entry in parse_page_sitemap(response.content):
            parts = urlsplit(entry.url)
            if parts.netloc.lower() != base.netloc.lower():
                continue                       # never follow links to other websites
            if parts.path in ("", "/") or parts.path.endswith(".xml"):
                continue                       # homepage / nested sitemaps
            # Always use our own scheme (never fetch plain http:// by accident).
            url = urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))
            pages.setdefault(url, PageEntry(url, entry.lastmod))

    if not pages:
        raise ScraperError("The sitemap contained no app pages.")
    return list(pages.values())


# --------------------------------------------------------------------------- #
# Step 2: read one app page
# --------------------------------------------------------------------------- #

def decode_download_href(href: str) -> str | None:
    """Turn ``https://site/dl?data=<base64>`` into the real file URL.

    The base64 decodes to something like ``url=https://archive.org/x.ipa&time=15``.
    Handles both ``==`` and ``%3D%3D`` padding, and missing padding.
    """
    values = parse_qs(urlsplit(href).query).get("data")
    if not values:
        return None
    data = values[0].replace(" ", "+")            # a raw "+" in a query string turns into " "
    data += "=" * (-len(data) % 4)                # repair missing padding
    try:
        decoded = base64.b64decode(data).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    # A regex (not parse_qs) keeps the URL's own %XX escapes exactly as written.
    match = re.search(r"(?:^|&)url=([^&]+)", decoded)
    return match.group(1) if match else None


def normalize_url(url: str) -> str:
    """Percent-encode characters that must not appear raw (e.g. spaces),
    leaving existing %XX escapes untouched."""
    parts = urlsplit(url.strip())
    safe = "/%:@!$&'()*+,;=~-._"
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        quote(parts.path, safe=safe),
        quote(parts.query, safe=safe + "?"),
        "",
    ))


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    if not allowed_hosts:
        return True
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


def validate_download_url(raw_url: str, allowed_hosts: tuple[str, ...]) -> tuple[str | None, str]:
    """Return (clean_url, "") if the link is acceptable, else (None, reason)."""
    url = normalize_url(raw_url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()

    if not host:
        return None, "invalid-url"
    if parts.username or parts.password:
        return None, "credentials-in-url"       # blocks https://archive.org@evil.example/ tricks
    if not _host_allowed(host, allowed_hosts):
        return None, f"host-not-allowed ({host})"
    if parts.scheme != "https":
        return None, "not-https"
    if not parts.path.lower().endswith(".ipa"):
        extension = Path(parts.path).suffix or "no extension"
        return None, f"not-a-direct-ipa ({extension})"
    return url, ""


def clean_version(raw: str) -> tuple[str, str]:
    """Return (version, marketing_version).

    The site sometimes writes things like "9.1.0 (T6.2.2) for iOS 16.1 or later".
    ``version`` keeps just the leading number ("9.1.0"); the full text is
    preserved as the marketing version.
    """
    raw = " ".join(raw.split())
    if not raw:
        return "1.0", "1.0"
    match = re.match(r"[vV]?(\d+(?:\.\d+)*)", raw)
    return (match.group(1) if match else raw), raw


def _meta(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    content = tag.get("content") if tag else None
    return content.strip() if content and content.strip() else None


def extract_name(soup: BeautifulSoup, page_url: str) -> str:
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    slug = Path(urlsplit(page_url).path.rstrip("/")).name
    return slug.replace("-", " ").title() or "Unknown App"


def extract_icon(soup: BeautifulSoup, page_url: str) -> str:
    candidates = []
    img = soup.select_one("img.img-fluid.rounded-5")
    if img:
        candidates += [img.get("data-src"), img.get("src")]   # lazy-loaded images keep the real URL in data-src
    candidates.append(_meta(soup, property="og:image"))
    for candidate in candidates:
        if candidate and not candidate.startswith("data:"):
            icon = urljoin(page_url, candidate.strip())
            return "https://" + icon[len("http://"):] if icon.startswith("http://") else icon
    return urljoin(page_url, "/favicon.ico")


def extract_developer_and_version(soup: BeautifulSoup) -> tuple[str, str]:
    developer = None
    version = None
    for element in soup.find_all("div", class_="ipa-developer"):
        text = element.get_text(strip=True)
        if "Version:" in text:
            version = text.replace("Version:", "").strip()
        elif "bi-person-fill" in str(element):    # the developer row carries a "person" icon
            developer = text
    if version is None:                           # fallback: plain-text search
        match = re.search(r"Version:\s*(\S[^\n]*)", soup.get_text("\n", strip=True))
        version = match.group(1).strip() if match else ""
    return developer or "Unknown Developer", version


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def extract_release_date(soup: BeautifulSoup, sitemap_lastmod: str | None) -> str:
    """Prefer the page's own 'modified' timestamp, then the sitemap's, then the
    visible 'Last updated: MM/DD/YYYY' text. (The old script used today's date,
    which made every app look freshly updated on every run.)"""
    for value in (_meta(soup, property="article:modified_time"), sitemap_lastmod):
        parsed = _parse_iso_date(value)
        if parsed:
            return parsed.isoformat()
    match = re.search(r"Last updated:\s*(\d{1,2})/(\d{1,2})/(\d{4})", soup.get_text(" ", strip=True))
    if match:
        month, day, year = map(int, match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date().isoformat()


def extract_description(soup: BeautifulSoup, name: str, page_url: str) -> str:
    text = _meta(soup, property="og:description") or _meta(soup, name="description") or ""
    text = re.sub(r"\s*(\[…\]|\[\.\.\.\]|…)\s*$", "…", " ".join(text.split()))[:600]
    return f"{text}\n\nSource page: {page_url}" if text else f"{name}\n\nSource page: {page_url}"


def make_bundle_id(page_url: str) -> str:
    """Stable, unique ID derived from the page address (e.g. me.ipalibrary.utm).
    Page addresses are unique, so two apps can never collide."""
    slug = unquote(urlsplit(page_url).path.strip("/")).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return BUNDLE_ID_PREFIX + slug


def scrape_page(entry: PageEntry, settings: Settings) -> tuple[AppRecord | None, str]:
    """Download and parse one page. Returns (app, "") or (None, reason_skipped)."""
    try:
        response = fetch(entry.url, settings.request_delay)
    except requests.RequestException as exc:
        log.warning("Network error for %s: %s", entry.url, exc)
        return None, "network-error"
    if response.status_code != 200:
        return None, f"http-{response.status_code}"

    soup = BeautifulSoup(response.content, "html.parser")

    link = soup.find("a", href=re.compile(r"/dl\?data="))
    if link is None:
        return None, "no-download-link"            # normal for non-app posts

    raw_url = decode_download_href(link["href"])
    if raw_url is None:
        return None, "undecodable-download-link"

    download_url, problem = validate_download_url(raw_url, settings.allowed_hosts)
    if download_url is None:
        return None, problem

    name = extract_name(soup, entry.url)
    developer, raw_version = extract_developer_and_version(soup)
    version, marketing_version = clean_version(raw_version)

    return AppRecord(
        page_url=entry.url,
        name=name,
        bundle_id=make_bundle_id(entry.url),
        developer=developer,
        version=version,
        marketing_version=marketing_version,
        release_date=extract_release_date(soup, entry.lastmod),
        download_url=download_url,
        icon_url=extract_icon(soup, entry.url),
        description=extract_description(soup, name, entry.url),
    ), ""


# --------------------------------------------------------------------------- #
# Step 3: assemble, check and write the source file
# --------------------------------------------------------------------------- #

def scrape_all(pages: list[PageEntry], settings: Settings) -> tuple[list[AppRecord], Counter]:
    apps: list[AppRecord] = []
    skipped: Counter = Counter()
    total = len(pages)
    log.info("Scraping %d pages with %d workers...", total, settings.workers)

    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        # map() yields results in input order, so output is deterministic.
        for done, (app, reason) in enumerate(pool.map(lambda e: scrape_page(e, settings), pages), 1):
            if app:
                apps.append(app)
            else:
                skipped[reason] += 1
            if done % 100 == 0 or done == total:
                log.info("  %d/%d pages done", done, total)
    return apps, skipped


def build_source(apps: list[AppRecord], source_url: str | None, base_url: str) -> dict:
    entries = [a.to_source_entry() for a in sorted(apps, key=lambda a: (a.name.casefold(), a.bundle_id))]

    seen = {e["bundleIdentifier"] for e in entries}
    if len(seen) != len(entries):  # AltStore rejects sources with duplicate bundle IDs
        raise ScraperError("Duplicate bundle identifiers were generated; refusing to write the file.")

    source = {
        "name": SOURCE_NAME,
        "identifier": SOURCE_IDENTIFIER,
        "subtitle": f"Unofficial index of ipalibrary.me ({len(entries)} apps)",
        "description": (
            "Unofficial, automatically generated AltStore/SideStore source listing the "
            f"apps published on {base_url}. Not affiliated with iPA Library."
        ),
        "website": base_url,
        "tintColor": TINT_COLOR,
        "apps": entries,
        "news": [],
    }
    if source_url:
        source["sourceURL"] = source_url
    return source


def previous_app_count(path: Path) -> int | None:
    try:
        return len(json.loads(path.read_text(encoding="utf-8"))["apps"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def check_result_is_sane(new_count: int, previous: int | None, min_apps: int, min_ratio: float) -> None:
    if new_count < min_apps:
        raise ScraperError(f"Only {new_count} apps found (minimum {min_apps}). "
                           "The website layout may have changed or the site is blocking us.")
    if previous and new_count < previous * min_ratio:
        raise ScraperError(f"App count dropped from {previous} to {new_count} "
                           f"(more than {round((1 - min_ratio) * 100)}% loss). Keeping the old file.")


def write_json_atomically(data: dict, path: Path) -> None:
    """Write to a temp file first, then swap it in, so a crash can't leave a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def summarize(apps: list[AppRecord], skipped: Counter) -> None:
    log.info("")
    log.info("Kept %d apps.", len(apps))
    if skipped:
        log.info("Skipped pages:")
        for reason, count in skipped.most_common():
            log.info("  %5d  %s", count, reason)
    hosts = Counter(urlsplit(a.download_url).hostname for a in apps)
    log.info("Download hosts in the output:")
    for host, count in hosts.most_common():
        log.info("  %5d  %s", count, host)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an AltStore/SideStore source from ipalibrary.me")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="where to write the JSON (default: next to this script)")
    parser.add_argument("--source-url", default=os.environ.get("SOURCE_URL"), help="public URL where the JSON will be hosted (stored inside the file)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)  # for tests
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="parallel downloads (1-16)")
    parser.add_argument("--allowed-hosts", default=os.environ.get("ALLOWED_DOWNLOAD_HOSTS"),
                        help="comma-separated hosts whose .ipa links are kept; use '*' for any. "
                             f"Default: {','.join(DEFAULT_ALLOWED_HOSTS)}")
    parser.add_argument("--min-apps", type=int, default=MIN_APPS, help="fail if fewer apps than this are found")
    parser.add_argument("--force", action="store_true", help="skip the sanity checks and write the file anyway")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY_SECONDS, help="seconds to pause after each request")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def parse_allowed_hosts(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return DEFAULT_ALLOWED_HOSTS
    if value.strip() == "*":
        return ()
    return tuple(h.strip().lower() for h in value.split(",") if h.strip())


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    settings = Settings(
        base_url=args.base_url.rstrip("/"),
        workers=max(1, min(args.workers, 16)),
        allowed_hosts=parse_allowed_hosts(args.allowed_hosts),
        request_delay=max(0.0, args.delay),
    )
    log.info("IPALibrary source generator")
    log.info("Allowed download hosts: %s", ", ".join(settings.allowed_hosts) or "(any)")

    try:
        pages = discover_app_pages(settings)
        log.info("Found %d candidate pages.", len(pages))
        apps, skipped = scrape_all(pages, settings)
        summarize(apps, skipped)

        if not args.force:
            check_result_is_sane(len(apps), previous_app_count(args.output), args.min_apps, MIN_RATIO_VS_PREVIOUS)

        source = build_source(apps, args.source_url, settings.base_url)
        write_json_atomically(source, args.output)
    except ScraperError as exc:
        log.error("ERROR: %s", exc)
        log.error("The existing output file (if any) was left untouched.")
        return 1

    log.info("Wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
