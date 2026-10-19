import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from lc_reconcile import Index, Source, build, parse_record
from lc_reconcile.store import Writer

FIXTURES = Path(__file__).parent / "fixtures"

VERNE = "http://id.loc.gov/authorities/names/n79064013"


@pytest.fixture(autouse=True)
def offline(request, monkeypatch):
    """Fail loudly rather than slowly if a test reaches the network.

    Tests that mean to reach the network say so with the ``network`` or
    ``live`` marker.
    """
    if {"network", "live"} & set(request.keywords):
        return

    def forbidden(*args, **kwargs):
        raise AssertionError("this test reached the network; stub urlopen")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)


@pytest.fixture(scope="session")
def index_path(tmp_path_factory) -> Path:
    """An index of the fixture snapshots, plus Verne from his own document.

    Verne comes in through an upsert, the way a sync will add records, since
    his record is a single-record document rather than a snapshot block.
    """
    path = tmp_path_factory.mktemp("index") / "lc.sqlite"
    build(
        path,
        [
            Source(scheme, str(FIXTURES / f"{scheme}.nt"), "2026-10-14T00:00:00+00:00")
            for scheme in ("names", "subjects", "genreForms")
        ],
    )
    record = parse_record(VERNE, (FIXTURES / "n79064013.nt").open(encoding="utf-8"))
    assert record is not None
    writer = Writer(path)
    writer.upsert(record)
    writer.close()
    return path


@pytest.fixture
def index(index_path) -> Iterator[Index]:
    index = Index(index_path)
    yield index
    index.close()
