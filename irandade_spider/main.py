import json
from pathlib import Path
from typing import Optional

import typer
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from irandade_spider.config import Settings
from irandade_spider.spiders.site_spider import SiteSpider

app = typer.Typer(
    name="spider",
    help="Crawl websites and download PDF/XLSX files",
)


@app.command()
def crawl(
    urls: list[str] = typer.Argument(
        ..., help="Root URLs to start crawling from"
    ),
    depth: Optional[int] = typer.Option(
        None, "--depth", "-d", help="Maximum crawl depth"
    ),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="Download root directory override"
    ),
    rate_limit: Optional[float] = typer.Option(
        None, "--rate-limit", "-r", help="Max requests per second"
    ),
    concurrent: Optional[int] = typer.Option(
        None, "--concurrent", "-c", help="Concurrent requests"
    ),
):
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

    domain = urls[0].split("//")[-1].split("/")[0] if urls else "unknown"
    state_path = download_root / domain / "meta" / "crawl_state.json"

    process = CrawlerProcess(settings=scrapy_settings)
    process.crawl(
        SiteSpider,
        start_urls=urls,
        max_depth=depth if depth is not None else cfg.default_depth,
        download_root=str(download_root),
        rate_limit=rate_limit if rate_limit is not None else cfg.rate_limit,
        state_path=state_path,
    )

    typer.echo(f"Starting crawl: {len(urls)} URLs")
    typer.echo(f"Download root: {download_root}")
    typer.echo(f"Max depth: {depth if depth is not None else cfg.default_depth}")
    typer.echo(f"Rate limit: {rate_limit if rate_limit is not None else cfg.rate_limit} req/s")
    typer.echo(f"State: {'resuming' if state_path.exists() else 'fresh start'}")

    process.start()


if __name__ == "__main__":
    app()
