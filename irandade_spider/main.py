import json
import sys
from pathlib import Path

import click
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from irandade_spider.config import Settings
from irandade_spider.spiders.site_spider import SiteSpider


@click.command()
@click.argument("urls", nargs=-1, required=True)
@click.option("--depth", "-d", type=int, default=None, help="Maximum crawl depth")
@click.option("--output-dir", "-o", type=Path, default=None, help="Download root directory")
@click.option("--rate-limit", "-r", type=float, default=None, help="Max requests per second")
@click.option("--concurrent", "-c", type=int, default=None, help="Concurrent requests")
def crawl(urls, depth, output_dir, rate_limit, concurrent):
    """Crawl websites and download PDF/XLSX files."""
    cfg = Settings()

    download_root = (
        output_dir.expanduser().resolve()
        if output_dir
        else cfg.download_root.expanduser().resolve()
    )

    ua_list = cfg.user_agents
    if isinstance(ua_list, str):
        ua_list = json.loads(ua_list)

    scrapy_settings = get_project_settings()
    scrapy_settings["DOWNLOAD_ROOT"] = str(download_root)
    scrapy_settings["CONCURRENT_REQUESTS"] = concurrent or cfg.concurrent_requests
    scrapy_settings["DOWNLOAD_DELAY"] = cfg.download_delay
    scrapy_settings["RANDOMIZE_DOWNLOAD_DELAY"] = cfg.randomize_delay
    scrapy_settings["SPIDER_USER_AGENTS"] = ua_list
    scrapy_settings["ROBOTSTXT_OBEY"] = cfg.respect_robots_txt

    url_list = list(urls)
    domain = url_list[0].split("//")[-1].split("/")[0] if url_list else "unknown"
    state_path = download_root / domain / "meta" / "crawl_state.json"

    process = CrawlerProcess(settings=scrapy_settings)
    process.crawl(
        SiteSpider,
        start_urls=url_list,
        max_depth=depth if depth is not None else cfg.default_depth,
        download_root=str(download_root),
        rate_limit=rate_limit if rate_limit is not None else cfg.rate_limit,
        state_path=state_path,
    )

    click.echo(f"Starting crawl: {len(url_list)} URLs")
    click.echo(f"Download root: {download_root}")
    click.echo(f"Max depth: {depth if depth is not None else cfg.default_depth}")
    click.echo(f"Rate limit: {rate_limit if rate_limit is not None else cfg.rate_limit} req/s")
    click.echo(f"State: {'resuming' if state_path.exists() else 'fresh start'}")

    process.start()


def entry():
    crawl()


if __name__ == "__main__":
    entry()
