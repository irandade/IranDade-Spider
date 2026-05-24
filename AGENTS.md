# IranDade Spider

Scrapy-based spider CLI that crawls websites, discovers PDF/Excel files (.pdf, .xlsx, .xls, .xlsm, .xlsb), downloads them preserving site directory structure, and supports idempotent re-runs with cache-skip and refresh modes.

## Quick Start

```bash
uv sync
uv run spider crawl https://example.com --depth 3 --output-dir /data/downloads --rate-limit 2
```

## Tech Stack
- **Python 3.14+** with `.venv` (uv-managed)
- **Scrapy** for crawling
- **Typer** for CLI
- **Pydantic/Pydantic-Settings** for config & models

## Project Structure

```
irandade_spider/
├── __init__.py       # Package marker (empty)
├── __main__.py       # Enables `python -m irandade_spider`
├── config.py         # Settings (pydantic-settings, reads .env with SPIDER_ prefix)
├── models.py         # Pydantic DTOs (FileMeta, PageMeta, CrawlState)
├── items.py          # Scrapy Items (PageItem, FileItem)
├── middlewares.py    # RandomUA, RandomDelay, Resume middlewares
├── pipelines.py      # PagePipeline, FilePipeline, StatePipeline
├── handlers.py       # NoVerifyHTTPSDownloadHandler (skips SSL cert verification)
├── settings.py       # Scrapy settings defaults
├── main.py           # Typer CLI entry (spider command)
└── spiders/
    ├── __init__.py     # Spiders package marker (empty)
    └── site_spider.py  # Main spider (crawls to depth, discovers PDF/Excel files)
```

## CLI

```bash
spider crawl [OPTIONS] URLS...
```

### Arguments
| Arg | Description |
|-----|-------------|
| `URLS` | One or more root URLs to crawl (positional, required) |
| `--depth, -d` | Max crawl depth (default: 2, from SPIDER_DEFAULT_DEPTH) |
| `--output-dir, -o` | Download root directory (overrides SPIDER_DOWNLOAD_ROOT) |
| `--rate-limit, -r` | Max requests per second (overrides SPIDER_RATE_LIMIT) |
| `--concurrent, -c` | Concurrent requests (overrides SPIDER_CONCURRENT_REQUESTS) |
| `--refresh, -f` | Check cached URLs for updates via conditional requests (default: skip cached) |

## Storage Layout

```
<download_root>/<domain>/
  cache/
    crawl_state.json                           # URL -> meta_path index
    <path>/<to>/<page>.html                    # HTML discovery pages
    <path>/<to>/<page>.html.meta.json          # per-page metadata (side-by-side)
  files/
    <path>/<to>/<file>.pdf                     # actual files (site structure preserved)
    <path>/<to>/<file>.pdf.meta.json           # per-file metadata (side-by-side)
```

Domain names are normalized (port stripped), so `example.com:443` and `example.com` share the same folder.

Each `.meta.json` file contains the common fields above. **PageMeta** additionally includes:
```json
{
  "name": "page.html",
  "extension": "html",
  "url": "https://example.com/path/page",
  "referer": "https://example.com/",
  "accessed_at": "2026-05-21T10:00:00+00:00",
  "etag": "W/\"abc123\"",
  "last_modified": "Tue, 20 May 2026 12:00:00 GMT",
  "content_hash": "sha256:abc123...",
  "size": 2048,
  "depth": 1,
  "status_code": 200
}
```

## Config (.env)

```env
SPIDER_DOWNLOAD_ROOT=~/spider_downloads
SPIDER_DEFAULT_DEPTH=2
SPIDER_RATE_LIMIT=5
SPIDER_CONCURRENT_REQUESTS=8
SPIDER_DOWNLOAD_DELAY=1.0
SPIDER_RANDOMIZE_DELAY=True
SPIDER_RESPECT_ROBOTSTXT=True
SPIDER_USER_AGENTS=["...at least 7 UAs..."]
SPIDER_LOG_LEVEL=INFO
```

CLI options override .env values. See `.env.example` for all variables.

## Architecture

### Data Flow
```
Spider startup → _scan_cache() → build cached_urls dict from cache/ + files/
URLs → SiteSpider → start() →
  ├─ cached URL (no --refresh) → skip (no HTTP request)
  ├─ cached URL (--refresh) → conditional request → 304 dropped / 200 re-downloaded
  ├─ not cached → HTTP request →
  │   ├─ PageItem → PagePipeline → saves HTML + .html.meta.json (in cache/)
  │   ├─ FileItem → FilePipeline → saves file + .meta.json (in files/)
  │   └─ same-domain links → check cache → skip or request (recursive, up to max_depth)
  └─ depth increased → _rescan_leaf_pages() → parse cached leaf HTML for new links
                            → StatePipeline → writes crawl_state.json on spider close
```

### Idempotent Crawl
1. **Startup scan**: Walk `cache/` and `files/` for all `.meta.json` files, build URL→metadata index
2. **Cache-skip mode (default)**: URLs with cached content are skipped entirely (no HTTP request)
3. **Refresh mode (`--refresh`)**: Cached URLs get conditional requests (If-None-Match/If-Modified-Since)
   - 304 → dropped by ResumeMiddleware, no re-processing
   - 200 → re-downloaded, metadata updated
4. **Depth increase**: When `--depth` increases, cached leaf pages (at old max_depth) are re-parsed from disk for new links
5. **Periodic stats**: Every 10 URLs checked, INFO log with aggregated progress (checked, discovered, downloaded, skipped, rates)
6. **Incremental state saving**: `crawl_state.json` is written every 10 items (PageItem + FileItem) to prevent data loss on interruption

### Resume Mechanism
1. `crawl_state.json` maps URL → relative meta file path
2. `ResumeMiddleware` injects `If-None-Match`/`If-Modified-Since` from `request.meta["conditional"]`
3. Server responds 304 → middleware drops response (no re-processing)
4. Server responds 200 → pipeline updates meta JSON with new etag/hash
5. Uncached URLs are requested normally

### Middleware Pipeline (order)
| Priority | Middleware | Purpose |
|----------|-----------|---------|
| 400 | RandomUserAgentMiddleware | Rotate User-Agent per request |
| 500 | RandomDelayMiddleware | Random wait between requests (delay × 0.5–1.5) |
| 550 | ResumeMiddleware | Conditional requests from meta, drop 304s |

### Item Pipelines (order)
| Priority | Pipeline | Purpose |
|----------|----------|---------|
| 200 | PagePipeline | Save HTML + metadata (side-by-side in cache/) |
| 300 | FilePipeline | Save PDF/Excel + metadata (side-by-side in files/) |
| 1000 | StatePipeline | Build crawl_state.json index |

## Key Design Decisions
- **Scrapy over aiohttp**: Leverages Scrapy's middleware stack, retries, robots.txt, and concurrency control
- **Per-file JSON metadata instead of DB**: Simpler, no external dependencies, human-readable
- **`{filename}.meta.json` naming**: Clear association between file and its metadata, stored side-by-side
- **Side-by-side metadata**: `.meta.json` files sit next to their content files (in `cache/` for pages, `files/` for downloads)
- **`crawl_state.json` is written every 10 items**: `StatePipeline` tracks a counter and calls `_save_state()` periodically, then again on `close_spider()`. This ensures safe interruption without data loss.
- **DOWNLOAD_ROOT in Scrapy settings**: Custom setting read by pipelines, not a built-in Scrapy setting
- **Same-domain restriction**: Spider only follows links within allowed_domains (derived from start URLs)
- **Port-stripped domains**: `example.com:443` and `example.com` share the same folder
- **HTTP error passthrough**: Spider allows 304, 404, 403, 500 responses through for processing (`HTTPERROR_ALLOWED_CODES`)
- **Media redirects enabled**: `MEDIA_ALLOW_REDIRECTS` set in spider custom settings
- **`--rate-limit` is stored on the spider but not actively enforced**: Actual throttling is controlled by Scrapy's `DOWNLOAD_DELAY` and `CONCURRENT_REQUESTS` settings
- **SSL verification disabled**: Custom `NoVerifyHTTPSDownloadHandler` skips certificate validation for sites with invalid/self-signed certs
- **Configurable log level**: `SPIDER_LOG_LEVEL` in `.env` controls Scrapy logging verbosity
- **Idempotent by design**: Re-runs skip cached content, no unnecessary downloads or HTTP requests
- **Async spider methods**: `start()`, `parse()`, `parse_file()`, `_maybe_request()`, `_process_links()`, `_rescan_leaf_pages()` are all async generators for Scrapy 2.16+ compatibility

## Development

```bash
uv sync                                # Install all dependencies
uv run spider --help                   # Verify CLI
uv run python -m pytest                # Run tests (if any exist)
```

## Edge Cases & Error Handling
- 304 responses: dropped by ResumeMiddleware, not re-processed
- Non-HTTP links (mailto, tel, javascript): filtered in spider.parse()
- Missing file extensions: infer from Content-Type header via ensure_ext()
- Failed requests: logged via errback, spider continues
- Server errors (5xx): retried up to 3 times via Scrapy retry middleware
- HTTP 404/403/500: passed through to spider for processing (not dropped)
- Empty paths (root URL): saved as index.html
- Corrupted `.meta.json`: logged as warning, skipped during cache scan
- Cached HTML missing but meta.json exists: treated as not cached, re-requested
- Depth decreased: deeper cached pages ignored, no rescan
- Depth same: normal cache-skip behavior, no rescan
