from datetime import datetime

from pydantic import BaseModel


class BaseMeta(BaseModel):
    name: str
    extension: str
    url: str
    referer: str | None = None
    accessed_at: datetime
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    size: int | None = None


class FileMeta(BaseMeta):
    pass


class PageMeta(BaseMeta):
    depth: int
    status_code: int


class CrawlState(BaseModel):
    version: str = "1"
    domain: str
    root_urls: list[str]
    last_crawl: datetime | None = None
    max_depth: int = 0
    urls: dict[str, str] = {}
