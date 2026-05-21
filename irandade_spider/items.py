import scrapy


class PageItem(scrapy.Item):
    url = scrapy.Field()
    body = scrapy.Field()
    content_type = scrapy.Field()
    depth = scrapy.Field()
    referer = scrapy.Field()
    response_headers = scrapy.Field()


class FileItem(scrapy.Item):
    url = scrapy.Field()
    body = scrapy.Field()
    content_type = scrapy.Field()
    referer = scrapy.Field()
    content_disposition = scrapy.Field()
    response_headers = scrapy.Field()
