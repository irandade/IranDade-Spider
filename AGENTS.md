# IranDade Spider

Scrapy-based spider CLI that crawls websites, discovers PDF/XLSX files, downloads them preserving site directory structure, and resumes without redownloading.

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
├── config.py         # Settings (pydantic-settings, reads .env with SPIDER_ prefix)
├── models.py         # Pydantic DTOs (FileMeta, PageMeta, CrawlState)
├── items.py          # Scrapy Items (PageItem, FileItem)
├── middlewares.py    # RandomUA, RandomDelay, Resume middlewares
├── pipelines.py      # PagePipeline, FilePipeline, StatePipeline
├── settings.py       # Scrapy settings defaults
├── main.py           # Typer CLI entry (spider crawl command)
└── spiders/
    └── site_spider.py  # Main spider (crawls to depth, discovers PDF/XLSX)
```

## CLI

```bash
spider crawl URLS... [--depth | -d] [--output-dir | -o] [--rate-limit | -r] [--concurrent | -c]
```

### Arguments
| Arg | Description |
|-----|-------------|
| `URLS` | One or more root URLs to crawl (positional, required) |
| `--depth, -d` | Max crawl depth (default: 2, from SPIDER_DEFAULT_DEPTH) |
| `--output-dir, -o` | Download root directory (overrides SPIDER_DOWNLOAD_ROOT) |
| `--rate-limit, -r` | Max requests per second (overrides SPIDER_RATE_LIMIT) |
| `--concurrent, -c` | Concurrent requests (overrides SPIDER_CONCURRENT_REQUESTS) |

## Storage Layout

```
<download_root>/<domain>/
  meta/
    crawl_state.json                           # URL -> meta_path index
    <path>/<to>/<file>.pdf.meta.json           # per-file metadata
    <path>/<to>/<page>.html.meta.json          # per-page metadata
  files/
    <path>/<to>/<file>.pdf                     # actual files (site structure preserved)
    <path>/<to>/<page>.html                    # HTML discovery pages
```

Each `.meta.json` file contains:
```json
{
  "name": "document.pdf",
  "extension": "pdf",
  "url": "https://example.com/path/document.pdf",
  "referer": "https://example.com/page",
  "accessed_at": "2026-05-21T10:00:00+00:00",
  "etag": "W/\"abc123\"",
  "last_modified": "Tue, 20 May 2026 12:00:00 GMT",
  "content_hash": "sha256:abc123...",
  "size": 2048576
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
SPIDER_USER_AGENTS=["...at least 7 UAs..."]
```

CLI options override .env values. See `.env.example` for all variables.

## Architecture

### Data Flow
```
URLs → SiteSpider → parse() →
  ├─ PageItem → PagePipeline → saves HTML + .html.meta.json
  ├─ PDF/XLSX links → parse_file() → FileItem → FilePipeline → saves file + .pdf.meta.json
  └─ same-domain links → parse() (recursive, up to max_depth)
                       → StatePipeline → writes crawl_state.json on spider close
```

### Resume Mechanism
1. `crawl_state.json` maps URL → relative meta file path
2. `ResumeMiddleware` injects `If-None-Match`/`If-Modified-Since` on repeat visits
3. Server responds 304 → middleware drops response (no re-processing)
4. Server responds 200 → pipeline updates meta JSON with new etag/hash
5. Unchanged pages/files are skipped entirely

### Middleware Pipeline (order)
| Priority | Middleware | Purpose |
|----------|-----------|---------|
| 400 | RandomUserAgentMiddleware | Rotate User-Agent per request |
| 500 | RandomDelayMiddleware | Random wait between requests |
| 550 | ResumeMiddleware | Conditional requests, drop 304s |

### Item Pipelines (order)
| Priority | Pipeline | Purpose |
|----------|----------|---------|
| 200 | PagePipeline | Save HTML + metadata |
| 300 | FilePipeline | Save PDF/XLSX + metadata |
| 1000 | StatePipeline | Build crawl_state.json index |

## Key Design Decisions
- **Scrapy over aiohttp**: Leverages Scrapy's middleware stack, retries, robots.txt, and concurrency control
- **Per-file JSON metadata instead of DB**: Simpler, no external dependencies, human-readable
- **`{filename}.meta.json` naming**: Clear association between file and its metadata
- **StatePipeline runs last (priority 1000)**: Ensures all items are processed before index is built
- **DOWNLOAD_ROOT in Scrapy settings**: Custom setting read by pipelines, not a built-in Scrapy setting
- **Same-domain restriction**: Spider only follows links within allowed_domains (derived from start URLs)

## Development

```bash
uv sync                                # Install all dependencies
uv run spider --help                   # Verify CLI
uv run python -m pytest                # Run tests
```

## Edge Cases & Error Handling
- 304 responses: dropped by ResumeMiddleware, not re-processed
- Non-HTTP links (mailto, tel, javascript): filtered in spider.parse()
- Missing file extensions: infer from Content-Type header via ensure_ext()
- Failed requests: logged via errback, spider continues
- Server errors (5xx): retried up to 3 times via Scrapy retry middleware
- Empty paths (root URL): saved as index.html
