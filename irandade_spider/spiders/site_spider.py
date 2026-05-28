import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin

import scrapy
from scrapy.http import Response
from scrapy.selector import Selector

from irandade_spider.items import PageItem, FileItem
from irandade_spider.models import CrawlState

FILE_EXTENSIONS = (
    ".pdf", ".xlsx", ".xls", ".xlsm", ".xlsb",
    ".doc", ".docx", ".zip", ".rar", ".ppt", ".pptx",".pps",".ppsx",
)
BLOCKED_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
    ".ico", ".tiff", ".tif",
    ".mp4", ".avi", ".mov", ".wmv", ".mkv", ".flv", ".webm",
    ".mp3", ".wav", ".aac", ".ogg", ".flac", ".wma",
    ".css", ".js",
)

_PORT_RE = re.compile(r":\d+$")
_STATS_INTERVAL = 10


def normalize_domain(netloc: str) -> str:
    return _PORT_RE.sub("", netloc)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # canonicalize scheme: treat http as https for deduplication
    if scheme in ("http", "https"):
        scheme = "https"
    # strip default ports
    if netloc.endswith(":443"):
        netloc = netloc[:-4]
    elif netloc.endswith(":80"):
        netloc = netloc[:-3]
    path = parsed.path
    if not path:
        path = "/"
    # rebuild URL without fragment, preserving query
    if parsed.query:
        return f"{scheme}://{netloc}{path}?{parsed.query}"
    return f"{scheme}://{netloc}{path}"


def is_file_url(url: str) -> bool:
    path = urlparse(url).path
    return path.lower().endswith(FILE_EXTENSIONS)


def is_blocked_url(url: str) -> bool:
    path = urlparse(url).path
    return path.lower().endswith(BLOCKED_EXTENSIONS)


def _is_file_content_type(content_type: str) -> bool:
    ct = content_type.lower()
    return (
        "pdf" in ct
        or "excel" in ct
        or "spreadsheet" in ct
        or "zip" in ct
        or "rar" in ct
        or "word" in ct
        or "msword" in ct
        or "powerpoint" in ct
        or "presentation" in ct
        or ct == "application/octet-stream"
    )


def _is_blocked_content_type(content_type: str) -> bool:
    ct = content_type.lower()
    return (
        ct.startswith("image/")
        or ct.startswith("video/")
        or ct.startswith("audio/")
        or "css" in ct
        or "javascript" in ct
        or ct == "text/javascript"
    )


def url_to_relative_path(url: str, max_bytes: int = 200) -> Path:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return Path("index.html")
    parts = path.split("/")
    last = parts[-1]
    if len(last.encode("utf-8")) > max_bytes:
        parts[-1] = hashlib.sha256(last.encode()).hexdigest()[:32]
    return Path("/".join(parts))


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
        self._url_state: dict[str, dict] = {}
        self._processed_urls: set[str] = set()

        self._start_time: float = 0
        self._last_stats_at: int = 0

        now = datetime.now()
        self._run_date = now.strftime("%Y%m%d")
        self._run_no = 0
        self._run_filename = f"run_{self._run_date}_{self._run_no}.json"
        self._pending_dirty_count = 0
        self._pending_flush_interval = 10

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
            self._load_url_state(old_state)

        self._load_run_state()

        scanned = 0
        for domain_dir in download_root.iterdir():
            if not domain_dir.is_dir():
                continue
            for subdir_name in ("caches", "files"):
                subdir = domain_dir / subdir_name
                if not subdir.exists():
                    continue
                for meta_path in subdir.rglob("*.meta.json"):
                    scanned += 1
                    if scanned % 1000 == 0:
                        self.logger.info(f"Cache scan: processed {scanned} meta files...")
                    try:
                        with open(meta_path, encoding="utf-8") as f:
                            meta = json.load(f)
                        url = meta.get("url")
                        if url:
                            self.cached_urls[normalize_url(url)] = meta
                    except (json.JSONDecodeError, OSError) as e:
                        self.logger.warning(f"Cache scan: skipping corrupted {meta_path}: {e}")

        self.crawl_stats["cached"] = len(self.cached_urls)
        self.logger.info(f"Cache scan: found {len(self.cached_urls)} cached URLs ({scanned} meta files scanned)")

    def _load_old_state(self) -> dict | None:
        if self.state_path and self.state_path.exists():
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                self.logger.warning(f"Failed to load crawl state: {e}")
        return None

    def _merge_url_state(self, url: str, info: dict):
        norm_url = normalize_url(url)
        existing = self._url_state.get(norm_url)
        if existing is None:
            self._url_state[norm_url] = info
            return
        # Prefer scrapped=True over False; keep earliest discovered_at
        if info.get("is_scrapped") is True:
            existing["is_scrapped"] = True
        if info.get("discovered_at") and (
            not existing.get("discovered_at")
            or info["discovered_at"] < existing["discovered_at"]
        ):
            existing["discovered_at"] = info["discovered_at"]
        # Use the smaller depth if available
        new_depth = info.get("depth")
        old_depth = existing.get("depth")
        if new_depth is not None and (old_depth is None or new_depth < old_depth):
            existing["depth"] = new_depth

    def _load_url_state(self, old_state: dict):
        url_files = old_state.get("url_files", [])
        if not url_files:
            return
        state_dir = self.state_path.parent if self.state_path else None
        if not state_dir:
            return
        for filename in url_files:
            path = state_dir / filename
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                    for url, info in data.items():
                        self._merge_url_state(url, info)
            except (json.JSONDecodeError, OSError) as e:
                self.logger.warning(f"Failed to load URL file {filename}: {e}")
        self.logger.debug(f"Loaded {len(self._url_state)} URL states from {len(url_files)} files")

    def _load_run_state(self):
        download_root = Path(self._download_root)
        if not download_root.exists():
            return
        for domain_dir in download_root.iterdir():
            if not domain_dir.is_dir():
                continue
            state_dir = domain_dir / "states"
            if not state_dir.exists():
                continue
            for f in sorted(state_dir.iterdir()):
                if not f.name.startswith("run_") or not f.name.endswith(".json"):
                    continue
                try:
                    with open(f, encoding="utf-8") as fh:
                        data = json.load(fh)
                    for url, info in data.items():
                        self._merge_url_state(url, info)
                except (json.JSONDecodeError, OSError) as e:
                    self.logger.warning(f"Failed to load run file {f}: {e}")

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

    def _mark_scrapped(self, url: str):
        if url in self._url_state:
            self._url_state[url]["is_scrapped"] = True
            self._pending_dirty_count += 1
            if self._pending_dirty_count >= self._pending_flush_interval:
                self._flush_run_state()

    def _record_discovered_url(self, url: str, depth: int):
        if url not in self._url_state:
            self._url_state[url] = {
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "is_scrapped": False,
                "depth": depth,
            }
            self._pending_dirty_count += 1
            if self._pending_dirty_count >= self._pending_flush_interval:
                self._flush_run_state()

    def _flush_run_state(self):
        if not self._url_state:
            return
        domains: dict[str, dict[str, dict]] = {}
        for url, info in self._url_state.items():
            domain = normalize_domain(urlparse(url).netloc)
            domains.setdefault(domain, {})[url] = info
        for domain, urls in domains.items():
            state_dir = Path(self._download_root) / domain / "states"
            state_dir.mkdir(parents=True, exist_ok=True)
            run_path = state_dir / self._run_filename
            with open(run_path, "w", encoding="utf-8") as f:
                json.dump(urls, f, indent=2, ensure_ascii=False)
        self._pending_dirty_count = 0

    async def _maybe_request(self, url: str, callback, depth: int = 0):
        norm_url = normalize_url(url)
        if norm_url in self._processed_urls:
            return
        self._processed_urls.add(norm_url)
        self.crawl_stats["checked"] += 1

        if norm_url in self.cached_urls:
            if self.refresh:
                meta = self.cached_urls[norm_url]
                conditional = {
                    "etag": meta.get("etag"),
                    "last_modified": meta.get("last_modified"),
                }
                self.crawl_stats["requested"] += 1
                self.logger.debug(f"Refresh request: {norm_url}")
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
                if is_file_url(norm_url):
                    self.crawl_stats["files_skipped"] += 1
                self.logger.debug(f"Skipping cached URL: {norm_url}")
                # Record cached URLs in state so they are not re-discovered on resume
                if norm_url not in self._url_state:
                    self._url_state[norm_url] = {
                        "discovered_at": datetime.now(timezone.utc).isoformat(),
                        "is_scrapped": True,
                        "depth": depth,
                    }
                    self._pending_dirty_count += 1
                    if self._pending_dirty_count >= self._pending_flush_interval:
                        self._flush_run_state()
        else:
            self.crawl_stats["requested"] += 1
            self.crawl_stats["discovered"] += 1
            self._record_discovered_url(norm_url, depth)
            self.logger.debug(f"Requesting new URL: {norm_url}")
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
            url = normalize_url(response.urljoin(href))
            parsed = urlparse(url)
            if not parsed.scheme.startswith("http"):
                continue
            if normalize_domain(parsed.netloc) not in self.allowed_domains:
                continue
            if is_blocked_url(url):
                continue

            is_file_link = is_file_url(url)
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
        skipped_count = 0
        reread_count = 0
        for idx, (url, meta) in enumerate(leaf_pages, 1):
            self.crawl_stats["checked"] += 1
            if idx % 100 == 0:
                self.logger.info(
                    f"Rescan: processed {idx}/{len(leaf_pages)} leaf pages "
                    f"({skipped_count} skipped, {reread_count} re-read, {new_links_count} new links queued)"
                )
                self._log_periodic_stats()

            domain = normalize_domain(urlparse(url).netloc)
            download_root = Path(self._download_root)
            max_bytes = self.settings.getint("SPIDER_MAX_PATH_FILENAME_BYTES", 200)
            cache_file = download_root / domain / "caches" / url_to_relative_path(url, max_bytes=max_bytes)

            if not cache_file.exists():
                self._mark_scrapped(url)
                self.logger.debug(f"Rescan: cached HTML missing for {url}")
                continue

            try:
                html = cache_file.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                self._mark_scrapped(url)
                self.logger.warning(f"Rescan: cannot read {cache_file}: {e}")
                continue

            reread_count += 1
            sel = Selector(text=html)
            for href in sel.css("a::attr(href)").getall():
                full_url = normalize_url(urljoin(url, href))

                parsed = urlparse(full_url)
                if not parsed.scheme.startswith("http"):
                    continue
                if normalize_domain(parsed.netloc) not in self.allowed_domains:
                    continue
                if is_blocked_url(full_url):
                    continue

                if full_url in self._processed_urls:
                    continue
                self._processed_urls.add(full_url)
                self.crawl_stats["checked"] += 1

                is_file_link = is_file_url(full_url)
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
                        skipped_count += 1
                        if is_file_link:
                            self.crawl_stats["files_skipped"] += 1
                        self.logger.debug(f"Rescan: skipping cached URL: {full_url}")
                        if full_url not in self._url_state:
                            self._url_state[full_url] = {
                                "discovered_at": datetime.now(timezone.utc).isoformat(),
                                "is_scrapped": True,
                                "depth": leaf_depth + 1,
                            }
                            self._pending_dirty_count += 1
                            if self._pending_dirty_count >= self._pending_flush_interval:
                                self._flush_run_state()
                else:
                    self.crawl_stats["requested"] += 1
                    self.crawl_stats["discovered"] += 1
                    self._record_discovered_url(full_url, leaf_depth + 1)
                    self.logger.debug(f"Rescan: requesting new link: {full_url}")
                    yield scrapy.Request(
                        full_url,
                        callback=callback,
                        errback=self.errback,
                        meta={"depth": leaf_depth + 1},
                    )
                    new_links_count += 1

            self._mark_scrapped(url)

        self.logger.info(
            f"Depth increased {self._old_max_depth}→{self.max_depth}: "
            f"rescanned {len(leaf_pages)} leaf pages, found {new_links_count} new links"
        )

    async def _resume_unscrapped(self):
        unscrapped = [
            url for url, state in self._url_state.items()
            if state.get("is_scrapped") == False
            and url not in self._processed_urls
        ]
        if not unscrapped:
            self.logger.info("Resume: no unscrapped pages found")
            return

        max_bytes = self.settings.getint("SPIDER_MAX_PATH_FILENAME_BYTES", 200)
        resumed_count = 0
        skipped_count = 0
        reread_count = 0
        for idx, url in enumerate(unscrapped, 1):
            self._processed_urls.add(url)
            self.crawl_stats["checked"] += 1
            if idx % 100 == 0:
                self.logger.info(
                    f"Resume: processed {idx}/{len(unscrapped)} unscrapped URLs "
                    f"({skipped_count} cached skipped, {reread_count} re-read from disk, {resumed_count} new requests)"
                )
                self._log_periodic_stats()

            source_depth = self._url_state[url].get("depth", 0)

            is_file_link = is_file_url(url)
            callback = self.parse_file if is_file_link else self.parse

            if url in self.cached_urls:
                if is_file_link:
                    skipped_count += 1
                    self.crawl_stats["skipped"] += 1
                    self.crawl_stats["files_skipped"] += 1
                    self._mark_scrapped(url)
                    self.logger.debug(f"Resume: skipping cached file URL: {url}")
                    continue

                if source_depth >= self.max_depth:
                    skipped_count += 1
                    self._mark_scrapped(url)
                    self.logger.debug(f"Resume: cached page at max depth, marking done: {url}")
                    continue

                domain = normalize_domain(urlparse(url).netloc)
                download_root = Path(self._download_root)
                cache_file = download_root / domain / "caches" / url_to_relative_path(url, max_bytes=max_bytes)

                if not cache_file.exists():
                    self.logger.debug(f"Resume: cached meta exists but HTML missing for {url}")
                    yield scrapy.Request(
                        url,
                        callback=callback,
                        errback=self.errback,
                        meta={"depth": source_depth},
                    )
                    self._record_discovered_url(url, source_depth)
                    resumed_count += 1
                    continue

                try:
                    html = cache_file.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    self.logger.warning(f"Resume: cannot read {cache_file}: {e}")
                    continue

                reread_count += 1
                sel = Selector(text=html)
                for href in sel.css("a::attr(href)").getall():
                    full_url = normalize_url(urljoin(url, href))

                    parsed = urlparse(full_url)
                    if not parsed.scheme.startswith("http"):
                        continue
                    if normalize_domain(parsed.netloc) not in self.allowed_domains:
                        continue
                    if is_blocked_url(full_url):
                        continue

                    if full_url in self._url_state:
                        continue
                    if full_url in self._processed_urls:
                        continue
                    self._processed_urls.add(full_url)
                    self.crawl_stats["checked"] += 1

                    link_is_file = is_file_url(full_url)
                    link_callback = self.parse_file if link_is_file else self.parse

                    if full_url in self.cached_urls:
                        if self.refresh:
                            cached_meta = self.cached_urls[full_url]
                            conditional = {
                                "etag": cached_meta.get("etag"),
                                "last_modified": cached_meta.get("last_modified"),
                            }
                            self.crawl_stats["requested"] += 1
                            self.logger.debug(f"Resume refresh request: {full_url}")
                            yield scrapy.Request(
                                full_url,
                                callback=link_callback,
                                errback=self.errback,
                                meta={
                                    "depth": source_depth + 1,
                                    "conditional": conditional,
                                    "is_refresh": True,
                                },
                            )
                            resumed_count += 1
                        else:
                            self.crawl_stats["skipped"] += 1
                            skipped_count += 1
                            if link_is_file:
                                self.crawl_stats["files_skipped"] += 1
                            self.logger.debug(f"Resume: skipping cached URL: {full_url}")
                            if full_url not in self._url_state:
                                self._url_state[full_url] = {
                                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                                    "is_scrapped": True,
                                    "depth": source_depth + 1,
                                }
                                self._pending_dirty_count += 1
                                if self._pending_dirty_count >= self._pending_flush_interval:
                                    self._flush_run_state()
                    else:
                        self.crawl_stats["requested"] += 1
                        self.crawl_stats["discovered"] += 1
                        self._record_discovered_url(full_url, source_depth + 1)
                        self.logger.debug(f"Resume: requesting new link: {full_url}")
                        yield scrapy.Request(
                            full_url,
                            callback=link_callback,
                            errback=self.errback,
                            meta={"depth": source_depth + 1},
                        )
                        resumed_count += 1

                self._mark_scrapped(url)
            else:
                if source_depth > self.max_depth:
                    continue

                self.crawl_stats["requested"] += 1
                self.crawl_stats["discovered"] += 1
                self._record_discovered_url(url, source_depth)
                self.logger.debug(f"Resume: requesting uncached URL: {url}")
                yield scrapy.Request(
                    url,
                    callback=callback,
                    errback=self.errback,
                    meta={"depth": source_depth},
                )
                resumed_count += 1

        self.logger.info(
            f"Resume: rescanned {len(unscrapped)} unscrapped pages, "
            f"requested {resumed_count} URLs"
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

        async for req in self._resume_unscrapped():
            yield req

    async def parse(self, response: Response, **kwargs):
        response_url = normalize_url(response.url)
        if response.status == 304:
            self.logger.debug(f"304 not modified: {response_url}")
            self._log_periodic_stats()
            return

        depth = response.meta.get("depth", 0)
        is_refresh = response.meta.get("is_refresh", False)
        content_type = response.headers.get(b"Content-Type", b"").decode(errors="replace")
        is_file = is_file_url(response_url) or _is_file_content_type(content_type)

        if _is_blocked_content_type(content_type):
            self.logger.debug(f"Skipping blocked content type {content_type}: {response_url}")
            self._mark_scrapped(response_url)
            self._log_periodic_stats()
            return

        if is_file:
            if is_refresh:
                self.crawl_stats["updated"] += 1
            else:
                self.crawl_stats["downloaded"] += 1
                self.crawl_stats["files_downloaded"] += 1
            self.logger.debug(f"Downloaded file: {response_url}")
            yield FileItem(
                url=response_url,
                body=response.body,
                content_type=content_type,
                depth=depth,
                referer=response.request.headers.get(b"Referer", b"").decode(errors="replace") or None,
                content_disposition=response.headers.get(b"Content-Disposition", b"").decode(errors="replace") or None,
                response_headers=dict(response.headers),
            )
            self._mark_scrapped(response_url)
            self._log_periodic_stats()
            return

        if is_refresh:
            self.crawl_stats["updated"] += 1
            self.logger.debug(f"Updated cached page: {response_url}")
        else:
            self.crawl_stats["downloaded"] += 1
            self.logger.debug(f"Downloaded new page: {response_url}")

        page_item = PageItem(
            url=response_url,
            body=response.body,
            content_type=content_type,
            depth=depth,
            referer=response.request.headers.get(b"Referer", b"").decode(errors="replace") or None,
            status=response.status,
            response_headers=dict(response.headers),
        )
        yield page_item

        async for item in self._process_links(response, depth):
            yield item
        self._mark_scrapped(response_url)
        self._log_periodic_stats()

    async def parse_file(self, response: Response):
        response_url = normalize_url(response.url)
        if response.status == 304:
            self.logger.debug(f"304 not modified: {response_url}")
            self._log_periodic_stats()
            return

        depth = response.meta.get("depth", 0)
        is_refresh = response.meta.get("is_refresh", False)
        if is_refresh:
            self.crawl_stats["updated"] += 1
        else:
            self.crawl_stats["downloaded"] += 1
            self.crawl_stats["files_downloaded"] += 1

        yield FileItem(
            url=response_url,
            body=response.body,
            content_type=response.headers.get(b"Content-Type", b"").decode(errors="replace"),
            depth=depth,
            referer=response.request.headers.get(b"Referer", b"").decode(errors="replace") or None,
            content_disposition=response.headers.get(b"Content-Disposition", b"").decode(errors="replace") or None,
            response_headers=dict(response.headers),
        )
        self._mark_scrapped(response_url)
        self._log_periodic_stats()

    def closed(self, reason):
        self._flush_run_state()
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
