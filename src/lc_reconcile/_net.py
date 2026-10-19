"""HTTP to id.loc.gov."""

from __future__ import annotations

import urllib.request
from http.client import HTTPResponse
from importlib.metadata import PackageNotFoundError, version

try:
    _VERSION = version("lc-reconcile")
except PackageNotFoundError:  # running from a source tree that isn't installed
    _VERSION = "0"

#: id.loc.gov asks clients to identify themselves.
DEFAULT_USER_AGENT = (
    f"lc-reconcile/{_VERSION} (+https://github.com/blue-core-lod/lc-reconcile)"
)

#: A snapshot is a large download, so allow a slow first byte.
TIMEOUT = 60.0


def open_url(url: str, *, user_agent: str = DEFAULT_USER_AGENT) -> HTTPResponse:
    """Open ``url`` for streaming. The caller closes the response."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    return urllib.request.urlopen(request, timeout=TIMEOUT)
