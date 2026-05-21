import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from scrapy import Spider
from scrapy.exceptions import DropItem

from irandade_spider.items import PageItem, FileItem
from irandade_spider.models import FileMeta, PageMeta, CrawlState


def url_to_relative_path(url: str) -> Path:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return Path("index.html")
    return Path(path)


def ensure_ext(path: Path, content_type: str) -> Path:
    if not path.suffix:
        if "html" in content_type:
            path = path.with_suffix(".html")
        elif "pdf" in content_type:
            path = path.with_suffix(".pdf")
        elif "spreadsheet" in content_type or "excel" in content_type:
            path = path.with_suffix(".xlsx")
    return path


def get_extension(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix if suffix else "unknown"


class PagePipeline:
    def __init__(self, download_root: Path):
        self.download_root = download_root

    @classmethod
    def from_crawler(cls, crawler):
        root = Path(crawler.settings["DOWNLOAD_ROOT"]).expanduser()
        return cls(root)

    def process_item(self, item, spider: Spider):
        if not isinstance(item, PageItem):
            return item

        domain = urlparse(item["url"]).netloc
        rel_path = url_to_relative_path(item["url"])
        rel_path = ensure_ext(rel_path, item.get("content_type", "text/html"))

        files_dir = self.download_root / domain / "files"
        meta_dir = self.download_root / domain / "meta"

        file_path = files_dir / rel_path
        meta_path = meta_dir / f"{rel_path}.meta.json"

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
            etag=item["response_headers"].get(b"Etag", b"").decode(errors="replace") or None,
            last_modified=item["response_headers"].get(b"Last-Modified", b"").decode(errors="replace") or None,
            content_hash=content_hash,
            size=file_path.stat().st_size,
            depth=item.get("depth", 0),
            status_code=int(item["response_headers"].get(b"Status", b"200").decode(errors="replace") or 200),
        )

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

        spider.logger.info(f"Saved page: {file_path}")
        return item


class FilePipeline:
    def __init__(self, download_root: Path):
        self.download_root = download_root

    @classmethod
    def from_crawler(cls, crawler):
        root = Path(crawler.settings["DOWNLOAD_ROOT"]).expanduser()
        return cls(root)

    def process_item(self, item, spider: Spider):
        if not isinstance(item, FileItem):
            return item

        domain = urlparse(item["url"]).netloc
        rel_path = url_to_relative_path(item["url"])

        files_dir = self.download_root / domain / "files"
        meta_dir = self.download_root / domain / "meta"

        file_path = files_dir / rel_path
        meta_path = meta_dir / f"{rel_path}.meta.json"

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
            etag=item["response_headers"].get(b"Etag", b"").decode(errors="replace") or None,
            last_modified=item["response_headers"].get(b"Last-Modified", b"").decode(errors="replace") or None,
            content_hash=content_hash,
            size=file_path.stat().st_size,
        )

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta.model_dump(mode="json"), f, indent=2, ensure_ascii=False)

        spider.logger.info(f"Saved file: {file_path}")
        return item


class StatePipeline:
    def __init__(self, download_root: Path):
        self.download_root = download_root
        self.seen: dict[str, str] = {}

    @classmethod
    def from_crawler(cls, crawler):
        root = Path(crawler.settings["DOWNLOAD_ROOT"]).expanduser()
        return cls(root)

    def process_item(self, item, spider: Spider):
        domain = urlparse(item["url"]).netloc
        rel_path = url_to_relative_path(item["url"])

        if isinstance(item, PageItem):
            rel_path = ensure_ext(rel_path, item.get("content_type", "text/html"))

        meta_rel = f"{rel_path}.meta.json"
        self.seen[item["url"]] = str(meta_rel)
        return item

    def close_spider(self, spider):
        domains: dict[str, dict[str, str]] = {}
        for url, meta_rel in self.seen.items():
            domain = urlparse(url).netloc
            domains.setdefault(domain, {})[url] = meta_rel

        for domain, urls in domains.items():
            state_dir = self.download_root / domain / "meta"
            state_dir.mkdir(parents=True, exist_ok=True)
            state = CrawlState(
                domain=domain,
                root_urls=list(getattr(spider, "start_urls", [])),
                last_crawl=datetime.now(timezone.utc),
                urls=urls,
            )
            state_path = state_dir / "crawl_state.json"
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state.model_dump(mode="json"), f, indent=2, ensure_ascii=False)
            spider.logger.info(f"State saved: {state_path} ({len(urls)} URLs)")
