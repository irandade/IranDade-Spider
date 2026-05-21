from pathlib import Path
from urllib.parse import urlparse

import scrapy
from scrapy.http import Response

from irandade_spider.items import PageItem, FileItem


FILE_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".xlsm", ".xlsb")


class SiteSpider(scrapy.Spider):
    name = "site_spider"

    custom_settings = {
        "MEDIA_ALLOW_REDIRECTS": True,
        "HTTPERROR_ALLOWED_CODES": [404, 403, 500],
    }

    def __init__(
        self,
        start_urls: list[str] | None = None,
        max_depth: int = 2,
        download_root: str | None = None,
        rate_limit: float | None = None,
        state_path: Path | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.start_urls = start_urls or []
        self.max_depth = max_depth
        self._download_root = download_root
        self.rate_limit = rate_limit
        self.state_path = state_path
        self.allowed_domains = [urlparse(u).netloc for u in self.start_urls]

    async def start(self):
        for url in self.start_urls:
            yield scrapy.Request(url, callback=self.parse, errback=self.errback)

    def parse(self, response: Response, **kwargs):
        depth = response.meta.get("depth", 0)

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

        is_file = response.url.lower().endswith(FILE_EXTENSIONS)
        if is_file:
            yield FileItem(
                url=response.url,
                body=response.body,
                content_type=response.headers.get(b"Content-Type", b"").decode(errors="replace"),
                referer=response.request.headers.get(b"Referer", b"").decode(errors="replace") or None,
                content_disposition=response.headers.get(b"Content-Disposition", b"").decode(errors="replace") or None,
                response_headers=dict(response.headers),
            )
            return

        if depth >= self.max_depth:
            return

        for href in response.css("a::attr(href)").getall():
            url = response.urljoin(href)
            parsed = urlparse(url)
            if not parsed.scheme.startswith("http"):
                continue
            if parsed.netloc not in self.allowed_domains:
                continue

            is_file_link = url.lower().endswith(FILE_EXTENSIONS)
            callback = self.parse_file if is_file_link else self.parse

            yield scrapy.Request(
                url,
                callback=callback,
                errback=self.errback,
            )

    def parse_file(self, response: Response):
        yield FileItem(
            url=response.url,
            body=response.body,
            content_type=response.headers.get(b"Content-Type", b"").decode(errors="replace"),
            referer=response.request.headers.get(b"Referer", b"").decode(errors="replace") or None,
            content_disposition=response.headers.get(b"Content-Disposition", b"").decode(errors="replace") or None,
            response_headers=dict(response.headers),
        )

    def errback(self, failure):
        self.logger.warning(f"Request failed: {failure.request.url}")
