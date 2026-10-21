"""Keep an index current from id.loc.gov's activity streams.

Each scheme publishes a feed of the records it has added, updated and
deprecated (https://id.loc.gov/authorities/{scheme}/activitystreams/feed),
newest page first. A sync reads back through it to a little before where the
last one stopped, fetches each changed record's N-Triples, and replaces what
the index holds for it. A record that no longer exists is removed. A
deprecated one is fetched like any other: it comes back as a
DeprecatedAuthority, and is indexed as one.

The feed's dates are not exact enough to resume from precisely. Items within
a page are not in order, and LCSH items are ``published`` on the date a list
of changes takes effect, which can be days in the future. So an item's date
is the later of its ``published`` and ``updated``, the sync looks back
:data:`MARGIN` before its cursor, and the cursor never passes today. Looking
at a change twice does no harm: it is applied the same way both times.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from lc_reconcile._net import Client
from lc_reconcile.records import ID_LOC, parse_record
from lc_reconcile.store import Writer

FEED_URL = "https://id.loc.gov/authorities/{scheme}/activitystreams/feed/{page}.json"

#: How far before its cursor a sync looks again.
MARGIN = timedelta(days=3)

#: An index whose cursor is older than this is rebuilt rather than synced:
#: the names feed has about ten pages a day, and a snapshot is quicker than
#: fetching a month of changes one record at a time.
MAX_AGE = timedelta(days=30)


class SyncError(Exception):
    """The index can't be synced as it stands."""


class RebuildNeeded(SyncError):
    """The index is too far behind to catch up from the feed."""


@dataclass(slots=True)
class SyncResult:
    scheme: str
    #: The date the sync looked back to.
    since: date
    pages: int
    updated: int
    deleted: int
    #: Where the next sync will start from.
    cursor: date
    #: False if the sync stopped at its time limit before applying everything.
    finished: bool = True


def item_date(item: dict[str, Any]) -> date | None:
    """The later of an activity's ``published`` and its object's ``updated``."""
    obj = item.get("object")
    values = [
        item.get("published"),
        obj.get("updated") if isinstance(obj, dict) else None,
    ]
    dates = []
    for value in values:
        if isinstance(value, str):
            try:
                dates.append(date.fromisoformat(value[:10]))
            except ValueError:
                continue
    return max(dates, default=None)


def read_feed(
    client: Client,
    scheme: str,
    since: date,
    on_page: Callable[[str], None] | None = None,
) -> tuple[dict[str, date], int]:
    """Records of ``scheme`` that changed on or after ``since``, with when.

    Pages are read until one holds nothing as recent as ``since``. Returns
    the changes and how many pages were read. ``on_page`` is told about each
    page read, and the oldest date on it.
    """
    prefix = f"{ID_LOC}/authorities/{scheme}/"
    changes: dict[str, date] = {}
    page = 0
    while True:
        page += 1
        body = client.get(FEED_URL.format(scheme=scheme, page=page))
        if body is None:  # past the last page
            return changes, page - 1
        data = json.loads(body)
        newest: date | None = None
        oldest: date | None = None
        for item in data.get("orderedItems") or []:
            when = item_date(item)
            obj = item.get("object")
            uri = obj.get("id") if isinstance(obj, dict) else None
            if when is None or not isinstance(uri, str):
                continue
            newest = when if newest is None else max(newest, when)
            oldest = when if oldest is None else min(oldest, when)
            if when >= since and uri.startswith(prefix):
                changes[uri] = max(changes.get(uri, when), when)
        if on_page:
            on_page(f"feed page {page}" + (f", back to {oldest}" if oldest else ""))
        if newest is None or newest < since or not data.get("next"):
            return changes, page


def record_url(uri: str) -> str:
    """Where to fetch a record's N-Triples."""
    return "https://" + uri.removeprefix("http://") + ".nt"


def sync(
    path: str | Path,
    schemes: Iterable[str] | None = None,
    *,
    client: Client | None = None,
    today: date | None = None,
    max_age: timedelta = MAX_AGE,
    time_limit: timedelta | None = None,
    progress: Callable[[str, str], None] | None = None,
    detail: Callable[[str, str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[SyncResult]:
    """Apply the changes LC has published since the index was last synced.

    Changes are applied oldest first, and the cursor is saved after each
    day's, so an interrupted sync picks up close to where it stopped. Once
    all are applied the cursor moves to today.

    With a ``time_limit``, the sync stops once it has run that long, as if
    interrupted, and leaves the schemes it hasn't reached for next time.

    ``progress`` is told when each scheme starts and after each day's
    changes; ``detail``, if given, is also told each feed page read and each
    record applied.
    """
    deadline = clock() + time_limit.total_seconds() if time_limit else None
    client = client or Client()
    today = today or datetime.now(UTC).date()
    wanted = set(schemes) if schemes is not None else None
    say = progress or (lambda scheme, message: None)
    say_more = detail or (lambda scheme, message: None)
    results = []
    writer = Writer(path)
    try:
        for status in writer.schemes():
            if wanted is not None and status.name not in wanted:
                continue
            start = status.cursor or status.snapshot_modified
            if not start:
                raise SyncError(
                    f"{status.name}: the index doesn't say when its snapshot was "
                    "made; rebuild it, giving --snapshot-date for local files"
                )
            cursor = date.fromisoformat(start[:10])
            if today - cursor > max_age:
                raise RebuildNeeded(
                    f"{status.name}: last synced to {cursor}, more than "
                    f"{max_age.days} days ago; rebuild it from a new snapshot"
                )
            since = cursor - MARGIN
            say(status.name, f"reading the feed back to {since}")
            changes, pages = read_feed(
                client, status.name, since, partial(say_more, status.name)
            )
            say(status.name, f"{len(changes):,} changed since {since} ({pages} pages)")

            ordered = sorted(changes.items(), key=lambda change: (change[1], change[0]))
            updated = deleted = 0
            for i, (uri, when) in enumerate(ordered):
                if deadline is not None and clock() >= deadline:
                    say(status.name, f"stopped at the time limit, synced to {cursor}")
                    results.append(
                        SyncResult(
                            status.name, since, pages, updated, deleted, cursor, False
                        )
                    )
                    return results
                body = client.get(record_url(uri))
                record = (
                    parse_record(uri, body.decode("utf-8").splitlines())
                    if body is not None
                    else None
                )
                if record is None:
                    writer.delete(uri)
                    deleted += 1
                    say_more(status.name, f"removed {uri}")
                else:
                    writer.upsert(record)
                    updated += 1
                    say_more(status.name, f"updated {uri} ({when})")
                last_of_day = i + 1 == len(ordered) or ordered[i + 1][1] != when
                if last_of_day:
                    cursor = max(cursor, min(when, today))
                    writer.set_cursor(status.name, cursor.isoformat(), finished=False)
                    say(
                        status.name, f"applied {i + 1:,} of {len(ordered):,}, to {when}"
                    )
            # Everything the feed holds since ``since`` is now applied, so the
            # next sync can start from today, even if nothing changed.
            cursor = max(cursor, today)
            writer.set_cursor(status.name, cursor.isoformat(), finished=True)
            results.append(
                SyncResult(status.name, since, pages, updated, deleted, cursor)
            )
    finally:
        writer.close()
    return results
