BOT_NAME = "irandade_spider"

SPIDER_MODULES = ["irandade_spider.spiders"]
NEWSPIDER_MODULE = "irandade_spider.spiders"

ROBOTSTXT_OBEY = True
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False

RETRY_ENABLED = True
RETRY_TIMES = 3
RETRY_HTTP_CODES = [500, 502, 503, 504, 408, 429]

DOWNLOADER_MIDDLEWARES = {
    "irandade_spider.middlewares.RandomUserAgentMiddleware": 400,
    "irandade_spider.middlewares.RandomDelayMiddleware": 500,
    "irandade_spider.middlewares.ResumeMiddleware": 550,
}

ITEM_PIPELINES = {
    "irandade_spider.pipelines.PagePipeline": 200,
    "irandade_spider.pipelines.FilePipeline": 300,
    "irandade_spider.pipelines.StatePipeline": 1000,
}

AUTOTHROTTLE_ENABLED = False
