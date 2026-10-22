"""Syncing an index from the activity streams, against a fake id.loc.gov."""

import json
import shutil
import sys
import urllib.error
from datetime import UTC, date, datetime, timedelta
from email.message import Message

import pytest

from lc_reconcile import Index, cli, normalize
from lc_reconcile._net import Client, FetchError
from lc_reconcile.store import Writer
from lc_reconcile.sync import (
    FEED_URL,
    RebuildNeeded,
    _counted,
    fetch_day,
    item_date,
    record_url,
    sync,
)

from .conftest import FIXTURES, VERNE

NAMES = "http://id.loc.gov/authorities/names/"
TODAY = date(2026, 10, 19)

NEW = NAMES + "n2026542348"  # Irwansyah, M. Rudi
KERN = NAMES + "n00000074"
SCHULZE = NAMES + "n00000022"
BRENNAN = NAMES + "n00000060"

MADS = "http://www.loc.gov/mads/rdf/v1#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# Kern, deprecated in favour of Brennan, the way id.loc.gov serves a
# deprecated record: the old heading is a variantLabel of the record itself.
KERN_DEPRECATED = f"""\
<{KERN}> <{RDF_TYPE}> <{MADS}PersonalName> .
<{KERN}> <{RDF_TYPE}> <{MADS}Variant> .
<{KERN}> <{RDF_TYPE}> <{MADS}DeprecatedAuthority> .
<{KERN}> <{MADS}variantLabel> "Kern, Andreas" .
<{KERN}> <{MADS}useInstead> <{BRENNAN}> .
<{KERN}> <{MADS}deletionNote> "Duplicate of n00000060." .
"""


def _item(type_, uri, published, updated=None):
    return {
        "type": type_,
        "published": published,
        "object": {"id": uri, "updated": updated or published},
    }


def _page(n, items, last=False):
    page = {"type": "OrderedCollectionPage", "orderedItems": items}
    if not last:
        page["next"] = FEED_URL.format(scheme="names", page=n + 1)
    return json.dumps(page).encode()


class FakeLC(Client):
    """Serves whatever ``pages`` holds by URL; anything else is a 404."""

    def __init__(self, pages, fail=()):
        super().__init__(delay=0, sleep=lambda seconds: None)
        self.pages = pages
        self.fail = set(fail)
        self.fetched = []

    def get(self, url):
        self.fetched.append(url)
        if url in self.fail:
            raise FetchError(url)
        return self.pages.get(url)


def _feed():
    verne = (FIXTURES / "n79064013.nt").read_text(encoding="utf-8")
    verne = verne.replace('"Verne, Jules, 1828-1905" .', '"Verne, Jules, 1828-1905!" .')
    return {
        FEED_URL.format(scheme="names", page=1): _page(
            1,
            [
                _item("Add", NEW, "2026-10-19"),
                _item("Update", VERNE, "2026-10-18"),
                # A second activity for the same record, out of order.
                _item("Update", VERNE, "2026-10-16"),
                _item(["Remove", "emm:Deprecate"], KERN, "2026-10-15"),
            ],
        ),
        FEED_URL.format(scheme="names", page=2): _page(
            2,
            [
                _item("Update", SCHULZE, "2026-10-12"),
                # Before the look-back: already in the snapshot.
                _item("Update", BRENNAN, "2026-10-01"),
            ],
        ),
        FEED_URL.format(scheme="names", page=3): _page(
            3, [_item("Update", BRENNAN, "2026-09-30")]
        ),
        FEED_URL.format(scheme="names", page=4): _page(
            4, [_item("Update", BRENNAN, "2026-09-29")], last=True
        ),
        record_url(NEW): (FIXTURES / "n2026542348.nt").read_bytes(),
        record_url(VERNE): verne.encode(),
        record_url(KERN): KERN_DEPRECATED.encode(),
        # Schulze is gone: not served, so a 404.
    }


class FixedNow(datetime):
    """A datetime whose now() is 15:00 UTC on the 20th, for the CLI's syncs."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 20, 15, 0, tzinfo=UTC)


@pytest.fixture
def fixed_now(monkeypatch):
    monkeypatch.setattr(sys.modules["lc_reconcile.sync"], "datetime", FixedNow)


@pytest.fixture
def index_copy(index_path, tmp_path):
    path = tmp_path / "lc.sqlite"
    shutil.copy(index_path, path)
    return path


def _status(path, name="names"):
    index = Index(path)
    try:
        return next(s for s in index.schemes() if s.name == name)
    finally:
        index.close()


def test_sync_applies_adds_updates_deprecations_and_removals(index_copy):
    before = _status(index_copy).records
    lc = FakeLC(_feed())
    [result] = sync(index_copy, ["names"], client=lc, today=TODAY)

    # The snapshot is of 2026-10-14, so the sync looks back to the 11th.
    # Page 3 is the first with nothing as recent, and the last read.
    assert result.since == date(2026, 10, 11)
    assert result.pages == 3
    assert (result.updated, result.deleted) == (3, 1)
    assert FEED_URL.format(scheme="names", page=4) not in lc.fetched
    assert record_url(BRENNAN) not in lc.fetched
    assert lc.fetched.count(record_url(VERNE)) == 1

    index = Index(index_copy)
    [new] = index.lookup(normalize("Irwansyah, M. Rudi"))
    assert (new.record.uri, new.via) == (NEW, "authorized")
    [verne] = index.lookup(normalize("Verne, Jules, 1828-1905!"))
    assert verne.record.uri == VERNE
    [kern] = index.lookup(normalize("Kern, Andreas"))
    assert (kern.record.uri, kern.via) == (KERN, "deprecated")
    assert index.use_instead(KERN) == [BRENNAN]
    assert index.get(SCHULZE) is None
    index.close()

    status = _status(index_copy)
    assert status.cursor == "2026-10-19"
    assert status.synced is not None
    assert status.records == before  # one added, one removed


def test_second_sync_changes_nothing(index_copy):
    sync(index_copy, ["names"], client=FakeLC(_feed()), today=TODAY)
    records = _status(index_copy).records
    [result] = sync(index_copy, ["names"], client=FakeLC(_feed()), today=TODAY)
    # It looks back from the new cursor, and the same changes come out the same.
    assert result.since == date(2026, 10, 16)
    assert _status(index_copy).records == records
    assert _status(index_copy).cursor == "2026-10-19"


def test_future_dates_are_refetched_and_not_trusted(index_copy):
    # LCSH publishes changes with the date they take effect, which can be
    # ahead of today.
    pages = {
        FEED_URL.format(scheme="names", page=1): _page(
            1, [_item("Update", VERNE, "2026-10-21", "2026-10-13")], last=True
        ),
        record_url(VERNE): (FIXTURES / "n79064013.nt").read_bytes(),
    }
    sync(index_copy, ["names"], client=FakeLC(pages), today=TODAY)
    assert _status(index_copy).cursor == "2026-10-19"
    lc = FakeLC(pages)
    sync(index_copy, ["names"], client=lc, today=TODAY)
    assert record_url(VERNE) in lc.fetched


def test_interrupted_sync_keeps_what_it_applied(index_copy):
    lc = FakeLC(_feed(), fail=[record_url(VERNE)])
    with pytest.raises(FetchError):
        sync(index_copy, ["names"], client=lc, today=TODAY)
    # Kern (the 15th) and Schulze (the 12th) were applied before Verne (the
    # 18th) failed; the cursor never goes back before the snapshot's date.
    status = _status(index_copy)
    assert status.cursor == "2026-10-15"
    assert status.synced is None
    index = Index(index_copy)
    assert index.get(SCHULZE) is None
    index.close()


def test_too_far_behind_to_sync(index_copy):
    with pytest.raises(RebuildNeeded, match="rebuild"):
        sync(index_copy, ["names"], client=FakeLC({}), today=date(2026, 12, 1))


def test_item_date():
    assert item_date(_item("Update", VERNE, "2026-10-21", "2026-10-13")) == date(
        2026, 10, 21
    )
    assert item_date({"object": {"updated": "2026-10-13T10:00:00"}}) == date(
        2026, 10, 13
    )
    assert item_date({"published": "soon"}) is None


def _http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://id.loc.gov/x", code, "", headers, None)


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_honours_retry_after(monkeypatch):
    replies = [_http_error(503, "7"), _Response(b"ok")]

    def urlopen(request, timeout):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    slept = []
    client = Client(delay=0.5, sleep=slept.append)
    assert client.get("https://id.loc.gov/x") == b"ok"
    assert slept == [7.0, 0.5]


def test_client_missing_and_failing(monkeypatch):
    errors = {"/gone": _http_error(404), "/broken": _http_error(500)}

    def urlopen(request, timeout):
        raise errors[request.full_url.removeprefix("https://id.loc.gov")]

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    client = Client(delay=0)
    assert client.get("https://id.loc.gov/gone") is None
    with pytest.raises(FetchError, match="HTTP 500"):
        client.get("https://id.loc.gov/broken")


def test_cli_sync_and_status(index_copy, monkeypatch, capsys, fixed_now):
    monkeypatch.setattr(cli, "Client", lambda delay: FakeLC(_feed()))
    db = str(index_copy)
    assert cli.main(["--db", db, "status"]) == 0
    assert "names: 4 records" in capsys.readouterr().out
    assert cli.main(["--db", db, "sync", "--scheme", "names"]) == 0
    err = capsys.readouterr().err
    assert "names: reading the feed back to 2026-10-11" in err
    assert "names: 4 records changed since 2026-10-11 (3 feed pages)" in err
    assert "names: 3 updated, 1 removed" in err
    assert "feed page 1" not in err
    assert cli.main(["--db", db, "status"]) == 0
    assert "synced to" in capsys.readouterr().out


def test_counted():
    assert _counted(1, "feed page") == "1 feed page"
    assert _counted(0, "record") == "0 records"
    assert _counted(4150, "record") == "4,150 records"


def test_cli_sync_verbose(index_copy, monkeypatch, capsys, fixed_now):
    monkeypatch.setattr(cli, "Client", lambda delay: FakeLC(_feed()))
    assert cli.main(["--db", str(index_copy), "sync", "-v"]) == 0
    err = capsys.readouterr().err
    assert "names: feed page 1, back to " in err
    assert "names: feed page 3, back to 2026-09-30" in err
    assert f"names: updated {VERNE} (2026-10-18)" in err
    assert f"names: removed {SCHULZE}" in err


def _fetched(path):
    writer = Writer(path)
    try:
        return dict(writer._db.execute("SELECT uri, day FROM fetched"))
    finally:
        writer.close()


def test_records_fetched_since_they_changed_are_not_fetched_again(index_copy):
    sync(index_copy, ["names"], client=FakeLC(_feed()), today=TODAY)
    # Everything was fetched on the 19th; the removal of Schulze counts too.
    assert set(_fetched(index_copy).values()) == {"2026-10-19"}

    lc = FakeLC(_feed())
    [result] = sync(index_copy, ["names"], client=lc, today=date(2026, 10, 20))
    # The look-back reaches the 16th: Verne (the 18th) was fetched since it
    # changed, but Irwansyah changed on the 19th, the day it was fetched,
    # and may have changed again after.
    assert record_url(VERNE) not in lc.fetched
    assert record_url(NEW) in lc.fetched
    assert (result.updated, result.skipped) == (1, 1)
    assert _fetched(index_copy)[NEW] == "2026-10-20"


def test_fetches_too_old_to_matter_are_forgotten(index_copy):
    sync(index_copy, ["names"], client=FakeLC(_feed()), today=TODAY)
    later = FakeLC({FEED_URL.format(scheme="names", page=1): _page(1, [], last=True)})
    sync(index_copy, ["names"], client=later, today=date(2026, 10, 30))
    # The next sync looks back only to the 27th.
    assert _fetched(index_copy) == {}


def test_an_index_from_before_fetches_were_kept(index_copy):
    writer = Writer(index_copy)
    writer._db.execute("DROP TABLE fetched")
    writer.close()
    [result] = sync(index_copy, ["names"], client=FakeLC(_feed()), today=TODAY)
    assert (result.updated, result.skipped) == (3, 0)


def test_fetch_day_is_the_earliest_date_anywhere():
    assert fetch_day(datetime(2026, 10, 22, 14, 57, tzinfo=UTC)) == date(2026, 10, 22)
    # Just after midnight UTC, it's still the 21st in the Americas.
    assert fetch_day(datetime(2026, 10, 22, 2, 0, tzinfo=UTC)) == date(2026, 10, 21)


def test_cli_says_what_is_up_to_date(index_copy, monkeypatch, capsys, fixed_now):
    db = str(index_copy)
    monkeypatch.setattr(cli, "Client", lambda delay: FakeLC(_feed()))
    sync(db, ["names"], client=FakeLC(_feed()), today=TODAY)
    # Synced to the 19th, so on the 20th it looks back to the 16th.
    assert cli.main(["--db", db, "sync", "--scheme", "names", "-v"]) == 0
    err = capsys.readouterr().err
    assert "names: 2 records changed since 2026-10-16 (2 feed pages)" in err
    assert (
        "names: 1 of them is up to date, fetched since they changed; 1 to fetch" in err
    )
    assert f"names: up to date: {VERNE} (changed 2026-10-18, fetched 2026-10-19)" in err
    assert (
        "names: 1 updated, 0 removed, 1 already up to date, synced to 2026-10-20" in err
    )


def test_time_limit(index_copy):
    # A clock that moves on a second each time it's read.
    ticks = iter(range(1000))
    [result] = sync(
        index_copy,
        ["names"],
        client=FakeLC(_feed()),
        today=TODAY,
        time_limit=timedelta(seconds=2.5),
        clock=lambda: next(ticks),
    )
    # It started at 0, applied Schulze (the 12th) at 1 and Kern (the 15th) at
    # 2, and stopped at 3.
    assert not result.finished
    assert (result.updated, result.deleted) == (1, 1)
    status = _status(index_copy)
    assert (status.cursor, status.synced) == ("2026-10-15", None)

    [result] = sync(index_copy, ["names"], client=FakeLC(_feed()), today=TODAY)
    assert result.finished
    assert _status(index_copy).cursor == "2026-10-19"
