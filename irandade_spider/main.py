import json
import re
from pathlib import Path

import typer
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings

from irandade_spider.config import Settings
from irandade_spider.spiders.site_spider import SiteSpider

_PORT_RE = re.compile(r":\d+$")


def normalize_domain(netloc: str) -> str:
    return _PORT_RE.sub("", netloc)

app = typer.Typer()


@app.callback()
def main():
    """IranDade Spider CLI."""
    pass


@app.command()
def crawl(
    urls: list[str] = typer.Argument(..., help="Root URLs to crawl"),
    depth: int | None = typer.Option(None, "--depth", "-d", help="Maximum crawl depth"),
    output_dir: Path | None = typer.Option(None, "--output-dir", "-o", help="Download root directory"),
    rate_limit: float | None = typer.Option(None, "--rate-limit", "-r", help="Max requests per second"),
    concurrent: int | None = typer.Option(None, "--concurrent", "-c", help="Concurrent requests"),
    refresh: bool = typer.Option(False, "--refresh", "-f", help="Check cached URLs for updates via conditional requests"),
):
    """Crawl websites and download PDF/Excel files."""
    if not urls:
        typer.echo("Error: at least one URL is required", err=True)
        raise typer.Exit(1)

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
    scrapy_settings["LOG_LEVEL"] = cfg.log_level

    url_list = list(urls)
    domain = normalize_domain(url_list[0].split("//")[-1].split("/")[0]) if url_list else "unknown"
    state_path = download_root / domain / "cache" / "crawl_state.json"

    process = CrawlerProcess(settings=scrapy_settings)
    process.crawl(
        SiteSpider,
        start_urls=url_list,
        max_depth=depth if depth is not None else cfg.default_depth,
        download_root=str(download_root),
        rate_limit=rate_limit if rate_limit is not None else cfg.rate_limit,
        state_path=state_path,
        refresh=refresh,
    )

    typer.echo(f"Starting crawl: {len(url_list)} URLs")
    typer.echo(f"Download root: {download_root}")
    typer.echo(f"Max depth: {depth if depth is not None else cfg.default_depth}")
    typer.echo(f"Rate limit: {rate_limit if rate_limit is not None else cfg.rate_limit} req/s")
    typer.echo(f"Mode: {'refresh' if refresh else 'cache-skip'}")
    typer.echo(f"State: {'resuming' if state_path.exists() else 'fresh start'}")

    process.start()


if __name__ == "__main__":
    app()
