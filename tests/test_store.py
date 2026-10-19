"""Upserts and deletes, which a sync applies to a built index."""

import shutil

from lc_reconcile import Index, normalize, parse_record
from lc_reconcile.store import Writer

from .conftest import FIXTURES, VERNE


def _verne():
    record = parse_record(VERNE, (FIXTURES / "n79064013.nt").open(encoding="utf-8"))
    assert record is not None
    return record


def _names(path):
    index = Index(path)
    try:
        return next(s.records for s in index.schemes() if s.name == "names")
    finally:
        index.close()


def test_upsert_replaces_labels(index_path, tmp_path):
    path = tmp_path / "lc.sqlite"
    shutil.copy(index_path, path)
    before = _names(path)

    record = _verne()
    record.label = "Verne, J. (Jules), 1828-1905"
    record.variants = []
    writer = Writer(path)
    writer.upsert(record)
    writer.close()

    index = Index(path)
    assert index.lookup(normalize("Verne, Jules, 1828-1905")) == []
    assert index.lookup(normalize("Verne, Julio, 1828-1905")) == []
    [hit] = index.lookup(normalize("Verne, J. (Jules), 1828-1905"))
    assert hit.record.uri == VERNE
    # The full-text index follows.
    assert index.search("julio") == []
    [(hit, found)] = index.search("verne j jules")
    assert found == normalize("Verne, J. (Jules), 1828-1905")
    index.close()
    # Replacing a record doesn't change how many there are.
    assert _names(path) == before


def test_delete(index_path, tmp_path):
    path = tmp_path / "lc.sqlite"
    shutil.copy(index_path, path)
    before = _names(path)

    writer = Writer(path)
    writer.delete(VERNE)
    writer.delete(VERNE)  # already gone: nothing to do
    writer.close()

    index = Index(path)
    assert index.get(VERNE) is None
    assert index.lookup(normalize("Verne, Jules, 1828-1905")) == []
    assert index.search("verne") == []
    index.close()
    assert _names(path) == before - 1
