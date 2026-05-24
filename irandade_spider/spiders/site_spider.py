import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urljoin

import scrapy
from scrapy.http import Response
from scrapy.selector import Selector

from irandade_spider.items import PageItem, FileItem
from irandade_spider.models import CrawlState

FILE_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".xlsm", ".xlsb")

_PORT_RE = re.compile(r":\d+$")
_STATS_INTERVAL = 10


def normalize_domain(netloc: str) -> str:
    return _PORT_RE.sub("", netloc)


def url_to_relative_path(url: str) -> Path:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return Path("index.html")
    return Path(path)


class SiteSpider(scrapy.Spider):
    name = "site_spider"

    custom_settings = {
        "MEDIA_ALLOW_REDIRECTS": True,
        "HTTPERROR_ALLOWED_CODES": [304, 404, 403, 500],
    }

    def __init__(
        self,
        start_urls: list[str] | None = None,
        max_depth: int = 2,
        download_root: str | None = None,
        rate_limit: float | None = None,
        state_path: Path | None = None,
        refresh: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.start_urls = start_urls or []
        self.max_depth = max_depth
        self._download_root = download_root
        self.rate_limit = rate_limit
        self.state_path = Path(state_path) if state_path else None
        self.refresh = refresh
        self.allowed_domains = [normalize_domain(urlparse(u).netloc) for u in self.start_urls]

        self.cached_urls: dict[str, dict] = {}
        self._old_max_depth: int = 0
        self._processed_urls: set[str] = set()

        self._start_time: float = 0
        self._last_stats_at: int = 0

        self.crawl_stats = {
            "cached": 0,
            "checked": 0,
            "discovered": 0,
            "requested": 0,
            "skipped": 0,
            "downloaded": 0,
            "updated": 0,
            "files_downloaded": 0,
            "files_skipped": 0,
        }

    def _scan_cache(self):
        download_root = Path(self._download_root) if self._download_root else None
        if not download_root or not download_root.exists():
            self.logger.info("Cache scan: no download root found, starting fresh")
            return

        old_state = self._load_old_state()
        if old_state:
            self._old_max_depth = old_state.get("max_depth", 0)

        for domain_dir in download_root.iterdir():
            if not domain_dir.is_dir():
                continue
            for subdir_name in ("cache", "files"):
                subdir = domain_dir / subdir_name
                if not subdir.exists():
                    continue
                for meta_path in subdir.rglob("*.meta.json"):
                    try:
                        with open(meta_path, encoding="utf-8") as f:
                            meta = json.load(f)
                        url = meta.get("url")
                        if url:
                            self.cached_urls[url] = meta
                    except (json.JSONDecodeError, OSError) as e:
                        self.logger.warning(f"Cache scan: skipping corrupted {meta_path}: {e}")

        self.crawl_stats["cached"] = len(self.cached_urls)
        self.logger.info(f"Cache scan: found {len(self.cached_urls)} cached URLs")

    def _load_old_state(self) -> dict | None:
        if self.state_path and self.state_path.exists():
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                self.logger.warning(f"Failed to load crawl state: {e}")
        return None

    def _log_periodic_stats(self):
        checked = self.crawl_stats["checked"]
        if checked - self._last_stats_at >= _STATS_INTERVAL:
            self._last_stats_at = checked
            elapsed = time.time() - self._start_time
            check_rate = checked / elapsed if elapsed > 0 else 0
            download_rate = self.crawl_stats["downloaded"] / elapsed if elapsed > 0 else 0

            self.logger.info(
                f"Progress [{checked} checked]: "
                f"{self.crawl_stats['discovered']} discovered, "
                f"{self.crawl_stats['downloaded']} downloaded ({self.crawl_stats['files_downloaded']} files), "
                f"{self.crawl_stats['skipped']} skipped ({self.crawl_stats['files_skipped']} files), "
                f"{check_rate:.1f} urls/s check, {download_rate:.1f} urls/s download"
            )

    async def _maybe_request(self, url: str, callback, depth: int = 0):
        if url in self._processed_urls:
            return
        self._processed_urls.add(url)
        self.crawl_stats["checked"] += 1

        if url in self.cached_urls:
            if self.refresh:
                meta = self.cached_urls[url]
                conditional = {
                    "etag": meta.get("etag"),
                    "last_modified": meta.get("last_modified"),
                }
                self.crawl_stats["requested"] += 1
                self.logger.debug(f"Refresh request: {url}")
                yield scrapy.Request(
                    url,
                    callback=callback,
                    errback=self.errback,
                    meta={
                        "depth": depth,
                        "conditional": conditional,
                        "is_refresh": True,
                    },
                )
            else:
                self.crawl_stats["skipped"] += 1
                if url.lower().endswith(FILE_EXTENSIONS):
                    self.crawl_stats["files_skipped"] += 1
                self.logger.debug(f"Skipping cached URL: {url}")
        else:
            self.crawl_stats["requested"] += 1
            self.crawl_stats["discovered"] += 1
            self.logger.debug(f"Requesting new URL: {url}")
            yield scrapy.Request(
                url,
                callback=callback,
                errback=self.errback,
                meta={"depth": depth},
            )

    async def _process_links(self, response: Response, depth: int):
        if depth >= self.max_depth:
            return

        for href in response.css("a::attr(href)").getall():
            url = response.urljoin(href)
            parsed = urlparse(url)
            if not parsed.scheme.startswith("http"):
                continue
            if normalize_domain(parsed.netloc) not in self.allowed_domains:
                continue

            is_file_link = url.lower().endswith(FILE_EXTENSIONS)
            callback = self.parse_file if is_file_link else self.parse

            async for req in self._maybe_request(url, callback, depth=depth + 1):
                yield req

    async def _rescan_leaf_pages(self):
        if self.max_depth <= self._old_max_depth:
            return

        leaf_depth = self._old_max_depth
        leaf_pages = [
            (url, meta)
            for url, meta in self.cached_urls.items()
            if meta.get("extension") == "html" and meta.get("depth") == leaf_depth
        ]

        if not leaf_pages:
            self.logger.info(f"Depth increased {self._old_max_depth}→{self.max_depth}: no leaf pages at depth {leaf_depth}")
            return

        new_links_count = 0
        for url, meta in leaf_pages:
            domain = normalize_domain(urlparse(url).netloc)
            download_root = Path(self._download_root)
            cache_file = download_root / domain / "cache" / url_to_relative_path(url)

            if not cache_file.exists():
                self.logger.debug(f"Rescan: cached HTML missing for {url}")
                continue

            try:
                html = cache_file.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self.logger.warning(f"Rescan: cannot read {cache_file}: {e}")
                continue

            sel = Selector(text=html)
            for href in sel.css("a::attr(href)").getall():
                full_url = urljoin(url, href)

                parsed = urlparse(full_url)
                if not parsed.scheme.startswith("http"):
                    continue
                if normalize_domain(parsed.netloc) not in self.allowed_domains:
                    continue

                if full_url in self._processed_urls:
                    continue
                self._processed_urls.add(full_url)
                self.crawl_stats["checked"] += 1

                is_file_link = full_url.lower().endswith(FILE_EXTENSIONS)
                callback = self.parse_file if is_file_link else self.parse

                if full_url in self.cached_urls:
                    if self.refresh:
                        cached_meta = self.cached_urls[full_url]
                        conditional = {
                            "etag": cached_meta.get("etag"),
                            "last_modified": cached_meta.get("last_modified"),
                        }
                        self.crawl_stats["requested"] += 1
                        self.logger.debug(f"Rescan refresh request: {full_url}")
                        yield scrapy.Request(
                            full_url,
                            callback=callback,
                            errback=self.errback,
                            meta={
                                "depth": leaf_depth + 1,
                                "conditional": conditional,
                                "is_refresh": True,
                            },
                        )
                        new_links_count += 1
                    else:
                        self.crawl_stats["skipped"] += 1
                        if is_file_link:
                            self.crawl_stats["files_skipped"] += 1
                        self.logger.debug(f"Rescan: skipping cached URL: {full_url}")
                else:
                    self.crawl_stats["requested"] += 1
                    self.crawl_stats["discovered"] += 1
                    self.logger.debug(f"Rescan: requesting new link: {full_url}")
                    yield scrapy.Request(
                        full_url,
                        callback=callback,
                        errback=self.errback,
                        meta={"depth": leaf_depth + 1},
                    )
                    new_links_count += 1

        self.logger.info(
            f"Depth increased {self._old_max_depth}→{self.max_depth}: "
            f"rescanned {len(leaf_pages)} leaf pages, found {new_links_count} new links"
        )

    async def start(self):
        self._start_time = time.time()
        self._scan_cache()

        for url in self.start_urls:
            async for req in self._maybe_request(url, self.parse, depth=0):
                yield req
            self._log_periodic_stats()

        async for req in self._rescan_leaf_pages():
            yield req

    async def parse(self, response: Response, **kwargs):
        if response.status == 304:
            self.logger.debug(f"304 not modified: {response.url}")
            self._log_periodic_stats()
            return

        depth = response.meta.get("depth", 0)
        is_refresh = response.meta.get("is_refresh", False)
        is_file = response.url.lower().endswith(FILE_EXTENSIONS)

        if is_file:
            if is_refresh:
                self.crawl_stats["updated"] += 1
            else:
                self.crawl_stats["downloaded"] += 1
                self.crawl_stats["files_downloaded"] += 1
            self.logger.debug(f"Downloaded file: {response.url}")
            yield FileItem(
                url=response.url,
                body=response.body,
                content_type=response.headers.get(b"Content-Type", b"").decode(errors="replace"),
                referer=response.request.headers.get(b"Referer", b"").decode(errors="replace") or None,
                content_disposition=response.headers.get(b"Content-Disposition", b"").decode(errors="replace") or None,
                response_headers=dict(response.headers),
            )
            return

        if is_refresh:
            self.crawl_stats["updated"] += 1
            self.logger.debug(f"Updated cached page: {response.url}")
        else:
            self.crawl_stats["downloaded"] += 1
            self.logger.debug(f"Downloaded new page: {response.url}")

        page_item = PageItem(
            url=response.url,
            body=response.body,
            content_type=response.headers.get(b"Content-Type", b"").decode(errors="replace"),
            depth=depth,
            referer=response.request.headers.get(b"Referer", b"").decode(errors="replace") or None,
            status=response.status,
            response_headers=dict(response.headers),
        )
        yield page_item

        async for item in self._process_links(response, depth):
            yield item
        self._log_periodic_stats()

    async def parse_file(self, response: Response):
        if response.status == 304:
            self.logger.debug(f"304 not modified: {response.url}")
            self._log_periodic_stats()
            return

        is_refresh = response.meta.get("is_refresh", False)
        if is_refresh:
            self.crawl_stats["updated"] += 1
        else:
            self.crawl_stats["downloaded"] += 1
            self.crawl_stats["files_downloaded"] += 1

        yield FileItem(
            url=response.url,
            body=response.body,
            content_type=response.headers.get(b"Content-Type", b"").decode(errors="replace"),
            referer=response.request.headers.get(b"Referer", b"").decode(errors="replace") or None,
            content_disposition=response.headers.get(b"Content-Disposition", b"").decode(errors="replace") or None,
            response_headers=dict(response.headers),
        )
        self._log_periodic_stats()

    def closed(self, reason):
        s = self.crawl_stats
        elapsed = time.time() - self._start_time
        check_rate = s["checked"] / elapsed if elapsed > 0 else 0
        download_rate = s["downloaded"] / elapsed if elapsed > 0 else 0

        self.logger.info(
            f"Crawl complete: {s['cached']} cached, {s['checked']} checked, "
            f"{s['discovered']} discovered, {s['requested']} requested, "
            f"{s['skipped']} skipped ({s['files_skipped']} files), "
            f"{s['downloaded']} downloaded ({s['files_downloaded']} files), "
            f"{s['updated']} updated"
        )
        self.logger.info(
            f"Rates: {check_rate:.2f} urls/s (check), {download_rate:.2f} urls/s (download), "
            f"elapsed {elapsed:.1f}s"
        )

    def errback(self, failure):
        self.logger.warning(f"Request failed: {failure.request.url}")
