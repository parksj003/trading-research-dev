"""Best-effort accounting for yfinance's curl_cffi HTTP traffic.

Scoped to the standalone optional EOD batch preparation; no request headers,
cookies, query strings, tickers, URL paths, or response bodies are recorded.
This measures only calls that traverse curl_cffi.requests.Session.request; it
does NOT assert complete network observability or override provider limits.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from threading import Lock
from urllib.parse import urlsplit


class CurlHttpMeter:
    def __init__(self):
        self._lock=Lock()
        self.attempts=0
        self.completed=0
        self.exceptions=0
        self.yahoo=0
        self.other=0
        self.statuses=Counter()

    def _record(self,kind,status=None):
        with self._lock:
            if kind=='attempt':
                self.attempts+=1
            elif kind=='yahoo':
                self.yahoo+=1
            elif kind=='other':
                self.other+=1
            elif kind=='exception':
                self.exceptions+=1
            elif kind=='response':
                self.completed+=1
                if isinstance(status,int):
                    self.statuses[str(status)]+=1

    def diagnostics(self):
        with self._lock:
            return dict(transport='curl_cffi.requests.Session.request',
                observed_attempts=self.attempts,
                observed_responses=self.completed,
                observed_exceptions=self.exceptions,
                observed_yahoo_requests=self.yahoo,
                observed_other_requests=self.other,
                observed_statuses=dict(sorted(self.statuses.items())),
                coverage='lower_bound_only',
                total_http_requests_known=False)


@contextmanager
def observe_yfinance_curl():
    """Instrumentation only; restore class method even if yf.download fails.

    The optional EOD workflow is single-purpose and prepares all yfinance
    batches BEFORE direct Yahoo calls, so this scope excludes direct calls.
    yfinance may use cached or other transports: observed zero never proves
    zero actual HTTP. Nothing is retried, redirected or bypassed here.
    """
    meter=CurlHttpMeter()
    from curl_cffi import requests as curl_requests
    original=curl_requests.Session.request

    def recorded_request(session,method,url,*args,**kwargs):
        meter._record('attempt')
        try:
            host=(urlsplit(str(url)).hostname or '').lower()
        except (TypeError,ValueError):
            host=''
        meter._record('yahoo' if host=='yahoo.com' or host.endswith('.yahoo.com') else 'other')
        try:
            response=original(session,method,url,*args,**kwargs)
        except Exception:
            meter._record('exception')
            raise
        meter._record('response',getattr(response,'status_code',None))
        return response

    curl_requests.Session.request=recorded_request
    try:
        yield meter
    finally:
        curl_requests.Session.request=original

