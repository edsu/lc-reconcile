"""Prebuilt indexes: packing one for download, and fetching one.

Building the names index takes a 7 GB download and a sync that can take
hours to catch up to today, so a built and synced index is published as a
GitHub release asset. A release asset must be under 2 GiB, and the index
is 2.7 GB gzipped, so it is published as one gzip stream split into parts,
with a manifest:

.. code-block:: json

    {
      "format": 1,
      "schema_version": 4,
      "created": "2026-10-19T06:17:00+00:00",
      "size": 8900000000,
      "sha256": "...",
      "schemes": [{"name": "names", "cursor": "2026-10-19", ...}],
      "parts": [{"name": "lc-reconcile-v4-20261019T061700Z.sqlite.gz.00",
                 "size": 2000000000, "sha256": "..."}]
    }

Parts are named relative to the manifest. There is one manifest per schema
version (``index-v4.json``), so an older lc-reconcile keeps finding an index
it can read after the schema changes.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import urllib.error
import urllib.parse
import zlib
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lc_reconcile import _net
from lc_reconcile.store import SCHEMA_VERSION, Index, IndexUnavailable

FORMAT = 1

#: Where ``fetch-index`` looks by default: the repository's ``index`` release.
RELEASE_URL = "https://github.com/blue-core-lod/lc-reconcile/releases/download/index/"

#: Under GitHub's limit of 2 GiB for a release asset.
PART_SIZE = 2_000_000_000

#: gzip's level 9 takes far longer over an index than 6, for little gain.
COMPRESSION = 6

_CHUNK = 1 << 20


class PrebuiltError(Exception):
    """A prebuilt index can't be fetched, or isn't what its manifest says."""


def manifest_name(schema_version: int = SCHEMA_VERSION) -> str:
    return f"index-v{schema_version}.json"


def default_manifest_url() -> str:
    return RELEASE_URL + manifest_name()


class _Parts:
    """A file-like sink that writes into numbered part files of a size.

    Each write opens its part to append to it, so no file is left open
    between writes; gzip writes in large enough pieces for that to be cheap.
    """

    def __init__(self, directory: Path, stem: str, part_size: int) -> None:
        self.directory = directory
        self.stem = stem
        self.part_size = part_size
        self.parts: list[dict[str, Any]] = []
        self._hash = hashlib.sha256()
        self._written = 0

    def write(self, data: bytes) -> int:
        view = memoryview(data)
        while view:
            if not self.parts or self._written == self.part_size:
                self._next()
            piece = view[: self.part_size - self._written]
            with open(self.directory / self.parts[-1]["name"], "ab") as part:
                part.write(piece)
            self._hash.update(piece)
            self._written += len(piece)
            self.parts[-1].update(size=self._written, sha256=self._hash.hexdigest())
            view = view[len(piece) :]
        return len(data)

    def flush(self) -> None:
        pass

    def _next(self) -> None:
        name = f"{self.stem}.{len(self.parts):02d}"
        (self.directory / name).unlink(missing_ok=True)
        self._hash = hashlib.sha256()
        self._written = 0
        self.parts.append({"name": name})


def pack(
    db: str | Path,
    directory: str | Path,
    *,
    part_size: int = PART_SIZE,
    now: datetime | None = None,
) -> Path:
    """Write ``db`` into ``directory`` as gzipped parts and a manifest.

    Returns the manifest's path. Nothing may be writing to ``db`` meanwhile.
    """
    db = Path(db)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with Index(db) as index:  # also checks it is an index of this version
        schemes = [asdict(status) for status in index.schemes()]
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    stem = f"lc-reconcile-v{SCHEMA_VERSION}-{now:%Y%m%dT%H%M%SZ}.sqlite.gz"

    whole = hashlib.sha256()
    size = 0
    parts = _Parts(directory, stem, part_size)
    with (
        open(db, "rb") as source,
        gzip.GzipFile(
            fileobj=parts, mode="wb", compresslevel=COMPRESSION, mtime=0
        ) as compressed,
    ):
        while chunk := source.read(_CHUNK):
            whole.update(chunk)
            size += len(chunk)
            compressed.write(chunk)

    manifest = {
        "format": FORMAT,
        "schema_version": SCHEMA_VERSION,
        "created": now.isoformat(),
        "size": size,
        "sha256": whole.hexdigest(),
        "schemes": schemes,
        "parts": parts.parts,
    }
    path = directory / manifest_name()
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def fetch(
    manifest: str,
    db: str | Path,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Download the index ``manifest`` describes to ``db``, replacing any there.

    ``manifest`` is a URL or a local path. The index is written beside ``db``
    and checked against the manifest before it is renamed into place, so a
    failed download leaves the old index as it was. Returns the manifest.
    """
    db = Path(db)
    description = _manifest(manifest)
    if description.get("format") != FORMAT:
        raise PrebuiltError(f"{manifest}: unknown format {description.get('format')}")
    if description.get("schema_version") != SCHEMA_VERSION:
        raise PrebuiltError(
            f"{manifest} is an index of schema version "
            f"{description.get('schema_version')}, this lc-reconcile needs "
            f"{SCHEMA_VERSION}"
        )

    partial = db.with_name(db.name + ".new")
    total = sum(part["size"] for part in description["parts"])
    done = 0
    whole = hashlib.sha256()
    size = 0
    decompressor = zlib.decompressobj(wbits=31)  # gzip
    try:
        with open(partial, "wb") as out:
            for part in description["parts"]:
                digest = hashlib.sha256()
                received = 0
                for chunk in _read_part(manifest, part["name"]):
                    digest.update(chunk)
                    received += len(chunk)
                    data = decompressor.decompress(chunk)
                    whole.update(data)
                    size += len(data)
                    out.write(data)
                    if progress:
                        progress(done + received, total)
                if received != part["size"] or digest.hexdigest() != part["sha256"]:
                    raise PrebuiltError(f"{part['name']} is not what {manifest} says")
                done += received
            data = decompressor.flush()
            whole.update(data)
            size += len(data)
            out.write(data)
        if not decompressor.eof:
            raise PrebuiltError(f"the index {manifest} describes is incomplete")
        if size != description["size"] or whole.hexdigest() != description["sha256"]:
            raise PrebuiltError(f"the index is not what {manifest} says")
        try:
            Index(partial).close()
        except IndexUnavailable as error:
            raise PrebuiltError(str(error)) from None
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, db)
    return description


def _manifest(location: str) -> dict[str, Any]:
    try:
        body = b"".join(_read(location))
    except FileNotFoundError:
        raise PrebuiltError(f"no prebuilt index at {location}") from None
    try:
        manifest = json.loads(body)
    except json.JSONDecodeError:
        raise PrebuiltError(f"{location} is not an index manifest") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("parts"), list):
        raise PrebuiltError(f"{location} is not an index manifest")
    return manifest


def _read_part(manifest: str, name: str) -> Iterator[bytes]:
    location = _resolve(manifest, name)
    try:
        yield from _read(location)
    except FileNotFoundError:
        raise PrebuiltError(f"{location} is missing") from None


def _resolve(manifest: str, name: str) -> str:
    if "://" in manifest:
        return urllib.parse.urljoin(manifest, urllib.parse.quote(name))
    return str(Path(manifest).with_name(name))


def _read(location: str) -> Iterator[bytes]:
    if "://" not in location:
        with open(location, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                yield chunk
        return
    try:
        response = _net.open_url(location)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise FileNotFoundError(location) from None
        raise PrebuiltError(f"{location}: HTTP {error.code}") from error
    except OSError as error:
        raise PrebuiltError(f"{location}: {error}") from error
    with response:
        try:
            while chunk := response.read(_CHUNK):
                yield chunk
        except OSError as error:
            raise PrebuiltError(f"{location}: {error}") from error
