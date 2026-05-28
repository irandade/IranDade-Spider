import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

from scrapy import Spider
from scrapy.exceptions import DropItem

from irandade_spider.items import PageItem, FileItem
from irandade_spider.models import FileMeta, PageMeta, CrawlState


_PORT_RE = re.compile(r":\d+$")


def normalize_domain(netloc: str) -> str:
    return _PORT_RE.sub("", netloc)


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


def ensure_ext(path: Path, content_type: str) -> Path:
    if not path.suffix:
        ct = content_type.lower()
        if "html" in ct:
            path = path.with_suffix(".html")
        elif "pdf" in ct:
            path = path.with_suffix(".pdf")
        elif "spreadsheet" in ct or "excel" in ct:
            path = path.with_suffix(".xlsx")
        elif "zip" in ct:
            path = path.with_suffix(".zip")
        elif "powerpoint" in ct or "presentation" in ct:
            path = path.with_suffix(".pptx")
        elif "word" in ct or "msword" in ct:
            path = path.with_suffix(".docx")
        elif "rar" in ct:
            path = path.with_suffix(".rar")
    return path


def get_orig_name(url: str, content_type: str, final_path: Path) -> str:
    parsed = urlparse(url)
    raw = parsed.path.strip("/")
    if not raw:
        return "index.html"
    name = unquote(raw.split("/")[-1])
    if final_path.suffix and not Path(name).suffix:
        name += final_path.suffix
    return name


_EXT_RE = re.compile(r"^[a-z0-9]+$")


def get_extension(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix and _EXT_RE.match(suffix):
        return suffix
    return "unknown"


def get_header(headers: dict, key: bytes, default: str | None = None) -> str | None:
    val = headers.get(key, default)
    if isinstance(val, list):
        val = val[0] if val else default
    if isinstance(val, bytes):
        val = val.decode(errors="replace")
    if not val:
        return None
    return str(val)


class PagePipeline:
    def __init__(self, download_root: Path, max_bytes: int = 200):
        self.download_root = download_root
        self.max_bytes = max_bytes
        self.crawler = None

    @classmethod
    def from_crawler(cls, crawler):
        root = Path(crawler.settings["DOWNLOAD_ROOT"]).expanduser()
        max_bytes = crawler.settings.getint("SPIDER_MAX_PATH_FILENAME_BYTES", 200)
        o = cls(root, max_bytes=max_bytes)
        o.crawler = crawler
        return o

    def process_item(self, item, spider=None):
        if not isinstance(item, PageItem):
            return item

        domain = normalize_domain(urlparse(item["url"]).netloc)
        rel_path = url_to_relative_path(item["url"], max_bytes=self.max_bytes)
        rel_path = ensure_ext(rel_path, item.get("content_type", "text/html"))

        cache_dir = self.download_root / domain / "caches"

        file_path = cache_dir / rel_path
        meta_path = cache_dir / f"{rel_path}.meta.json"

        file_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        body = item["body"]
        if isinstance(body, bytes):
            file_path.write_bytes(body)
        else:
            file_path.write_text(body, encoding="utf-8")

        content_hash = "sha256:" + hashlib.sha256(body if isinstance(body, bytes) else body.encode()).hexdigest()

        meta = PageMeta(
            name=rel_path.name,
            extension=get_extension(rel_path),
            url=item["url"],
            referer=item.get("referer"),
            accessed_at=datetime.now(timezone.utc),
            etag=get_header(item["response_headers"], b"Etag"),
            last_modified=get_header(item["response_headers"], b"Last-Modified"),
            content_hash=content_hash,
            size=file_path.stat().st_size,
            depth=item.get("depth", 0),
            status_code=int(item.get("status", 200)),
            orig_name=get_orig_name(item["url"], item.get("content_type", ""), rel_path),
        )

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

        active = spider or self.crawler.spider
        active.logger.info(f"Saved page: {file_path}")
        return item


class FilePipeline:
    def __init__(self, download_root: Path, max_bytes: int = 200):
        self.download_root = download_root
        self.max_bytes = max_bytes
        self.crawler = None

    @classmethod
    def from_crawler(cls, crawler):
        root = Path(crawler.settings["DOWNLOAD_ROOT"]).expanduser()
        max_bytes = crawler.settings.getint("SPIDER_MAX_PATH_FILENAME_BYTES", 200)
        o = cls(root, max_bytes=max_bytes)
        o.crawler = crawler
        return o

    def process_item(self, item, spider=None):
        if not isinstance(item, FileItem):
            return item

        domain = normalize_domain(urlparse(item["url"]).netloc)
        rel_path = url_to_relative_path(item["url"], max_bytes=self.max_bytes)
        rel_path = ensure_ext(rel_path, item.get("content_type", ""))

        files_dir = self.download_root / domain / "files"

        file_path = files_dir / rel_path
        meta_path = files_dir / f"{rel_path}.meta.json"

        file_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.parent.mkdir(parents=True, exist_ok=True)

        body = item["body"]
        if isinstance(body, bytes):
            file_path.write_bytes(body)
        else:
            file_path.write_bytes(body.encode())

        content_hash = "sha256:" + hashlib.sha256(body if isinstance(body, bytes) else body.encode()).hexdigest()

        meta = FileMeta(
            name=rel_path.name,
            extension=get_extension(rel_path),
            url=item["url"],
            referer=item.get("referer"),
            accessed_at=datetime.now(timezone.utc),
            etag=get_header(item["response_headers"], b"Etag"),
            last_modified=get_header(item["response_headers"], b"Last-Modified"),
            content_hash=content_hash,
            size=file_path.stat().st_size,
            depth=item.get("depth"),
            orig_name=get_orig_name(item["url"], item.get("content_type", ""), rel_path),
        )

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

        active = spider or self.crawler.spider
        active.logger.info(f"Saved file: {file_path}")
        return item


class StatePipeline:
    def __init__(self, download_root: Path):
        self.download_root = download_root
        self.seen: dict[str, dict] = {}
        self.crawler = None
        self._count = 0
        self._flush_interval = 10

    @classmethod
    def from_crawler(cls, crawler):
        root = Path(crawler.settings["DOWNLOAD_ROOT"]).expanduser()
        o = cls(root)
        o.crawler = crawler
        return o

    def process_item(self, item, spider=None):
        if not isinstance(item, PageItem):
            return item

        now = datetime.now(timezone.utc)
        self.seen[item["url"]] = {
            "discovered_at": now.isoformat(),
            "is_scrapped": False,
            "depth": item.get("depth", 0),
        }
        self._count += 1

        if self._count % self._flush_interval == 0:
            self._save_state(spider or self.crawler.spider)

        return item

    def _save_state(self, spider=None):
        domains: dict[str, dict[str, dict]] = {}
        for url, info in self.seen.items():
            domain = normalize_domain(urlparse(url).netloc)
            domains.setdefault(domain, {})[url] = info

        active = spider or self.crawler.spider
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d")

        for domain, urls in domains.items():
            state_dir = self.download_root / domain / "states"
            state_dir.mkdir(parents=True, exist_ok=True)

            url_items = list(urls.items())
            url_files: list[str] = []
            max_per_file = 1000

            for i in range(0, len(url_items), max_per_file):
                chunk = dict(url_items[i : i + max_per_file])
                no = i // max_per_file
                filename = f"urls_{date_str}_{no}.json"
                url_path = state_dir / filename
                with open(url_path, "w", encoding="utf-8") as f:
                    json.dump(chunk, f, indent=2, ensure_ascii=False)
                url_files.append(filename)

            state = CrawlState(
                domain=domain,
                root_urls=list(getattr(active, "start_urls", [])),
                last_crawl=now,
                max_depth=getattr(active, "max_depth", 0),
                url_files=url_files,
            )
            state_path = state_dir / "state.json"
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state.model_dump(mode="json"), f, indent=2, ensure_ascii=False)
        active.logger.info(f"State saved: {len(self.seen)} URLs tracked")

    def close_spider(self, spider=None):
        self._save_state(spider or self.crawler.spider)
