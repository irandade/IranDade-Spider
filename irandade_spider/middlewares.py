import random
import time
import json
from pathlib import Path
from typing import Optional

from scrapy import signals
from scrapy.http import Request, Response
from scrapy.spiders import Spider

from irandade_spider.models import CrawlState


class RandomUserAgentMiddleware:
    def __init__(self, user_agents: list[str]):
        self.user_agents = user_agents

    @classmethod
    def from_crawler(cls, crawler):
        ua_setting = crawler.settings.get("SPIDER_USER_AGENTS")
        if isinstance(ua_setting, str):
            ua_list = json.loads(ua_setting)
        elif isinstance(ua_setting, list):
            ua_list = ua_setting
        else:
            ua_list = crawler.settings.getlist("SPIDER_USER_AGENTS") or []
        o = cls(user_agents=ua_list)
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        return o

    def spider_opened(self, spider):
        spider.logger.info(f"RandomUA: loaded {len(self.user_agents)} agents")

    def process_request(self, request: Request, spider: Spider):
        request.headers["User-Agent"] = random.choice(self.user_agents)


class RandomDelayMiddleware:
    def __init__(self, delay: float, randomize: bool):
        self.delay = delay
        self.randomize = randomize

    @classmethod
    def from_crawler(cls, crawler):
        delay = crawler.settings.getfloat("DOWNLOAD_DELAY", 1.0)
        randomize = crawler.settings.getbool("RANDOMIZE_DOWNLOAD_DELAY", True)
        return cls(delay, randomize)

    def process_request(self, request: Request, spider: Spider):
        if self.randomize and self.delay > 0:
            wait = self.delay * random.uniform(0.5, 1.5)
            time.sleep(wait)


class ResumeMiddleware:
    def __init__(self):
        self.state_path: Optional[Path] = None
        self.crawl_state: Optional[CrawlState] = None

    @classmethod
    def from_crawler(cls, crawler):
        o = cls()
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        return o

    def spider_opened(self, spider):
        self.state_path = getattr(spider, "state_path", None)
        if self.state_path and self.state_path.exists():
            with open(self.state_path) as f:
                data = json.load(f)
                self.crawl_state = CrawlState(**data)
            spider.logger.info(
                f"Resume: loaded {len(self.crawl_state.urls)} URLs from previous crawl"
            )
        else:
            self.crawl_state = None
            spider.logger.info("Resume: no previous state found, starting fresh")

    def process_request(self, request: Request, spider: Spider):
        if self.crawl_state is None:
            return None
        url = request.url
        if url in self.crawl_state.urls:
            meta_path = self.state_path.parent / self.crawl_state.urls[url]
            if meta_path.exists():
                with open(meta_path) as f:
                    meta = json.load(f)
                if meta.get("etag"):
                    request.headers["If-None-Match"] = meta["etag"]
                if meta.get("last_modified"):
                    request.headers["If-Modified-Since"] = meta["last_modified"]
        return None

    def process_response(self, request: Request, response: Response, spider: Spider):
        if response.status == 304:
            spider.logger.debug(f"Resume: 304 not modified, dropping {request.url}")
            return None
        return response
