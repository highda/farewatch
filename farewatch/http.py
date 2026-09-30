"""Small polite HTTP client: honest UA, per-host throttle + jitter, backoff on 429/5xx, gzip, no URL logging of secrets."""
from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, url: str, detail: str = ""):
        super().__init__(f"HTTP {status} for {url.split('?')[0]} {detail}".strip())
        self.status = status


class Http:
    def __init__(self, user_agent: str, min_delay: float = 1.5, timeout: float = 30, retries: int = 3):
        self.user_agent, self.min_delay, self.timeout, self.retries = user_agent, min_delay, timeout, retries
        self.requests = 0                      # every attempt counts (conservative for budgets)
        self._last: dict[str, float] = {}
        self.sleep = time.sleep                # injectable for tests

    def _throttle(self, host: str, min_delay: float | None) -> None:
        wait = (self.min_delay if min_delay is None else min_delay) - (time.monotonic() - self._last.get(host, 0))
        if wait > 0:
            self.sleep(wait + random.uniform(0, 0.5))
        self._last[host] = time.monotonic()

    def request(self, url: str, params: dict | None = None, headers: dict | None = None,
                min_delay: float | None = None, retry_429: bool = True) -> tuple[int, dict, bytes]:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        host = urllib.parse.urlsplit(url).netloc
        hdrs = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip", **(headers or {})}
        for attempt in range(self.retries + 1):
            self._throttle(host, min_delay)
            self.requests += 1
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body, status, rh = r.read(), r.status, {k.lower(): v for k, v in r.headers.items()}
            except urllib.error.HTTPError as e:
                rh = {k.lower(): v for k, v in e.headers.items()}
                if e.code == 304:
                    return 304, rh, b""
                if e.code in (429, 500, 502, 503, 504) and (retry_429 or e.code != 429) and attempt < self.retries:
                    self.sleep(self._backoff(attempt, rh.get("retry-after")))
                    continue
                raise HttpError(e.code, url, e.read()[:200].decode("utf-8", "replace")) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    self.sleep(self._backoff(attempt, None))
                    continue
                raise HttpError(0, url, str(e)) from None
            if rh.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            return status, rh, body
        raise HttpError(0, url, "retries exhausted")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(int(retry_after), 60)
        return min(2 ** attempt * 3, 60) + random.uniform(0, 1)

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None,
                 min_delay: float | None = None, retry_429: bool = True):
        _, _, body = self.request(url, params, {"Accept": "application/json", **(headers or {})}, min_delay, retry_429)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise HttpError(200, url, f"invalid JSON: {e}") from None
