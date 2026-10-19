"""Syncing an index from the activity streams, against a fake id.loc.gov."""

import json
import shutil
import urllib.error
from datetime import date
from email.message import Message

import pytest

from lc_reconcile import Index, cli, normalize
from lc_reconcile._net import Client, FetchError
from lc_reconcile.sync import (
    FEED_URL,
    RebuildNeeded,
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


def test_cli_sync_and_status(index_copy, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda delay: FakeLC(_feed()))
    db = str(index_copy)
    assert cli.main(["--db", db, "status"]) == 0
    assert "names: 4 records" in capsys.readouterr().out
    assert cli.main(["--db", db, "sync", "--scheme", "names"]) == 0
    assert "names: 3 updated, 1 removed" in capsys.readouterr().err
    assert cli.main(["--db", db, "status"]) == 0
    assert "synced to" in capsys.readouterr().out
