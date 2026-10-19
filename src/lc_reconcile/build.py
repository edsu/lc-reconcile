"""Build an index from id.loc.gov bulk snapshots."""

from __future__ import annotations

import gzip
import io
import os
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from itertools import islice
from pathlib import Path
from typing import IO

from lc_reconcile import _net
from lc_reconcile.records import iter_records
from lc_reconcile.store import Writer

#: Schemes a build knows how to fetch. Each is a MADS/RDF N-Triples snapshot.
SCHEMES = (
    "names",
    "subjects",
    "genreForms",
    "childrensSubjects",
    "demographicTerms",
    "performanceMediums",
)

SNAPSHOT_URL = "https://id.loc.gov/download/authorities/{scheme}.madsrdf.nt.gz"

#: Records per insert transaction.
_BATCH = 10_000

_GZIP_MAGIC = b"\x1f\x8b"


@dataclass(slots=True)
class Source:
    """Where one scheme's snapshot comes from."""

    #: The scheme the snapshot should hold, or None to take it from the
    #: records themselves. When given, a snapshot of another scheme is refused.
    scheme: str | None
    #: A URL, or a path to a local copy (gzipped or not).
    location: str
    #: When LC last changed the snapshot, which is where a sync starts from.
    #: Read from the response for a URL; for a local file, say it yourself.
    modified: str | None = None


def default_sources(schemes: Sequence[str]) -> list[Source]:
    return [Source(s, SNAPSHOT_URL.format(scheme=s)) for s in schemes]


@contextmanager
def _open(source: Source) -> Generator[IO[str]]:
    if "://" in source.location:
        response = _net.open_url(source.location)
        try:
            modified = response.headers.get("Last-Modified")
            if modified and source.modified is None:
                source.modified = parsedate_to_datetime(modified).isoformat(
                    timespec="seconds"
                )
            yield _text(response, gzipped=source.location.endswith(".gz"))
        finally:
            response.close()
    else:
        with open(source.location, "rb") as handle:
            gzipped = handle.peek(2)[:2] == _GZIP_MAGIC
            yield _text(handle, gzipped=gzipped)


def _text(raw: IO[bytes], *, gzipped: bool) -> IO[str]:
    if gzipped:
        raw = io.BufferedReader(gzip.GzipFile(fileobj=raw))
    return io.TextIOWrapper(raw, encoding="utf-8", newline="\n")


def build(
    path: str | Path,
    sources: Sequence[Source],
    *,
    progress: Callable[[str, int], None] | None = None,
) -> None:
    """Build a new index at ``path`` from ``sources``, replacing any there.

    The index is written beside ``path`` and renamed into place only once it
    is complete, so a failed build leaves the old index as it was.
    """
    path = Path(path)
    partial = path.with_name(path.name + ".new")
    partial.unlink(missing_ok=True)
    writer = Writer(partial, create=True)
    try:
        writer.bulk_mode()
        for source in sources:
            seen: set[str] = set()
            with _open(source) as lines:
                records = iter_records(lines)
                total = 0
                while batch := list(islice(records, _BATCH)):
                    seen.update(record.scheme for record in batch)
                    if source.scheme and seen != {source.scheme}:
                        raise ValueError(
                            f"{source.location} holds {', '.join(sorted(seen))} "
                            f"records, not {source.scheme}"
                        )
                    total += writer.add(batch)
                    if progress:
                        progress(source.scheme or "/".join(sorted(seen)), total)
            for scheme in sorted(seen):
                writer.record_scheme(
                    scheme, source=source.location, snapshot_modified=source.modified
                )
        writer.create_indexes()
    except BaseException:
        writer.close()
        partial.unlink(missing_ok=True)
        raise
    writer.close()
    os.replace(partial, path)
