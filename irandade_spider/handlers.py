import ssl
from scrapy.core.downloader.handlers.http11 import HTTP11DownloadHandler


class NoVerifyTLSContextFactory:
    def get_context(self, uri=None):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


class NoVerifyHTTPSDownloadHandler(HTTP11DownloadHandler):
    def __init__(self, settings):
        super().__init__(settings)
        self._tls_context_factory = NoVerifyTLSContextFactory()
