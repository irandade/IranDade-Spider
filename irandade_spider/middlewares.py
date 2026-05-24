import random
import time
import json
from pathlib import Path
from typing import Optional

from scrapy import signals
from scrapy.http import Request, Response
from scrapy.spiders import Spider


class RandomUserAgentMiddleware:
    def __init__(self, user_agents: list[str]):
        self.user_agents = user_agents
        self.crawler = None

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
        o.crawler = crawler
        crawler.signals.connect(o.spider_opened, signal=signals.spider_opened)
        return o

    def spider_opened(self, spider):
        spider.logger.info(f"RandomUA: loaded {len(self.user_agents)} agents")

    def process_request(self, request: Request, spider=None):
        request.headers["User-Agent"] = random.choice(self.user_agents)


class RandomDelayMiddleware:
    def __init__(self, delay: float, randomize: bool):
        self.delay = delay
        self.randomize = randomize
        self.crawler = None

    @classmethod
    def from_crawler(cls, crawler):
        delay = crawler.settings.getfloat("DOWNLOAD_DELAY", 1.0)
        randomize = crawler.settings.getbool("RANDOMIZE_DOWNLOAD_DELAY", True)
        o = cls(delay, randomize)
        o.crawler = crawler
        return o

    def process_request(self, request: Request, spider=None):
        if self.randomize and self.delay > 0:
            wait = self.delay * random.uniform(0.5, 1.5)
            time.sleep(wait)


class ResumeMiddleware:
    def __init__(self):
        self.crawler = None

    @classmethod
    def from_crawler(cls, crawler):
        o = cls()
        o.crawler = crawler
        return o

    def process_request(self, request: Request, spider=None):
        conditional = request.meta.get("conditional")
        if conditional:
            if conditional.get("etag"):
                request.headers["If-None-Match"] = conditional["etag"]
            if conditional.get("last_modified"):
                request.headers["If-Modified-Since"] = conditional["last_modified"]
        return None

    def process_response(self, request: Request, response: Response, spider=None):
        if response.status == 304:
            active = spider or self.crawler.spider
            active.logger.debug(f"Resume: 304 not modified, dropping {request.url}")
            return None
        return response
