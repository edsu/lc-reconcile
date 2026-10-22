"""HTTP to id.loc.gov."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse
from importlib.metadata import PackageNotFoundError, version

try:
    _VERSION = version("lc-reconcile")
except PackageNotFoundError:  # running from a source tree that isn't installed
    _VERSION = "0"

#: id.loc.gov asks clients to identify themselves.
DEFAULT_USER_AGENT = f"lc-reconcile/{_VERSION} (+https://github.com/edsu/lc-reconcile)"

#: A snapshot is a large download, so allow a slow first byte.
TIMEOUT = 60.0

#: Longest we will honour a Retry-After, or back off, before trying again.
MAX_RETRY_WAIT = 60.0


def open_url(url: str, *, user_agent: str = DEFAULT_USER_AGENT) -> HTTPResponse:
    """Open ``url`` for streaming. The caller closes the response."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    return urllib.request.urlopen(request, timeout=TIMEOUT)


class FetchError(Exception):
    """A request failed for a reason other than the resource not existing."""


class Client:
    """Small GETs to id.loc.gov, spaced out and retried.

    A sync makes thousands of these, so they wait ``delay`` seconds between
    requests, and on a 429 or 503 wait as long as the service asks.
    """

    def __init__(
        self,
        *,
        delay: float = 0.5,
        max_retries: int = 4,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.requests = 0
        self._sleep = sleep

    def get(self, url: str) -> bytes | None:
        """The body at ``url``, or None if it doesn't exist (404 or 410)."""
        for attempt in range(self.max_retries + 1):
            if self.delay and self.requests:
                self._sleep(self.delay)
            self.requests += 1
            try:
                with open_url(url, user_agent=self.user_agent) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code in (404, 410):
                    return None
                # 429 and 503 are the service asking us to come back later.
                if error.code in (429, 503) and attempt < self.max_retries:
                    self._sleep(_retry_after(error, attempt))
                    continue
                raise FetchError(f"{url}: HTTP {error.code}") from error
            except OSError as error:  # URLError, timeouts, connection failures
                if attempt < self.max_retries:
                    self._sleep(min(2.0**attempt, MAX_RETRY_WAIT))
                    continue
                raise FetchError(f"{url}: {error}") from error
        raise AssertionError("unreachable")


def _retry_after(error: urllib.error.HTTPError, attempt: int) -> float:
    """How long the service asked us to wait, falling back to backing off."""
    header = error.headers.get("Retry-After") if error.headers else None
    if header:
        try:
            return min(max(float(header), 0.0), MAX_RETRY_WAIT)
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
            return min(max(seconds, 0.0), MAX_RETRY_WAIT)
    return min(2.0**attempt, MAX_RETRY_WAIT)
