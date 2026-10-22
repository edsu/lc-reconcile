"""Packing an index into parts, and fetching it back."""

import json
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

import pytest

from lc_reconcile import SCHEMES, Index, cli
from lc_reconcile.prebuilt import PrebuiltError, fetch, manifest_name, pack
from lc_reconcile.store import SCHEMA_VERSION

NOW = datetime(2026, 10, 19, 6, 17, tzinfo=UTC)


@pytest.fixture
def packed(index_path, tmp_path):
    # Small parts, so that the index is split into several.
    return pack(index_path, tmp_path / "out", part_size=4_000, now=NOW)


def _manifest(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_pack(index_path, packed):
    manifest = _manifest(packed)
    assert packed.name == manifest_name() == f"index-v{SCHEMA_VERSION}.json"
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["size"] == index_path.stat().st_size
    assert {s["name"] for s in manifest["schemes"]} == set(SCHEMES)
    parts = manifest["parts"]
    assert len(parts) > 1
    assert (
        parts[0]["name"]
        == f"lc-reconcile-v{SCHEMA_VERSION}-20261019T061700Z.sqlite.gz.00"
    )
    for part in parts:
        size = (packed.parent / part["name"]).stat().st_size
        assert size == part["size"] <= 4_000


def test_fetch_from_a_path(index_path, packed, tmp_path):
    db = tmp_path / "fetched.sqlite"
    seen = []
    fetch(str(packed), db, progress=lambda done, total: seen.append((done, total)))
    assert db.read_bytes() == index_path.read_bytes()
    assert seen[-1][0] == seen[-1][1]
    with Index(db) as index:
        assert index.get("http://id.loc.gov/authorities/names/n79064013") is not None


def _corrupt(path, at=100):
    data = bytearray(path.read_bytes())
    data[at] ^= 0xFF
    path.write_bytes(bytes(data))


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda out, parts: _corrupt(out / parts[1]["name"]), "is not what"),
        # The gzip header: the stream breaks before the part's checksum.
        (lambda out, parts: _corrupt(out / parts[0]["name"], at=0), "is not what"),
        (lambda out, parts: (out / parts[-1]["name"]).unlink(), "is missing"),
    ],
)
def test_a_bad_download_leaves_the_old_index(packed, tmp_path, damage, message):
    damage(packed.parent, _manifest(packed)["parts"])
    db = tmp_path / "lc.sqlite"
    db.write_bytes(b"the old index")
    with pytest.raises(PrebuiltError, match=message):
        fetch(str(packed), db)
    assert db.read_bytes() == b"the old index"
    assert not db.with_name("lc.sqlite.new").exists()


def test_another_schema_version_is_refused(packed, tmp_path):
    manifest = _manifest(packed)
    manifest["schema_version"] = SCHEMA_VERSION + 1
    packed.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PrebuiltError, match="schema version"):
        fetch(str(packed), tmp_path / "lc.sqlite")


def test_no_manifest(tmp_path):
    with pytest.raises(PrebuiltError, match="no prebuilt index"):
        fetch(str(tmp_path / "index-v3.json"), tmp_path / "lc.sqlite")


class _Response:
    def __init__(self, body):
        self.body = body

    def read(self, size=-1):
        chunk, self.body = (
            (self.body, b"") if size < 0 else (self.body[:size], self.body[size:])
        )
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_from_a_release(index_path, packed, tmp_path, monkeypatch):
    base = "https://github.com/edsu/lc-reconcile/releases/download/index/"
    served = {base + p.name: p.read_bytes() for p in packed.parent.iterdir()}
    requested = []

    def open_url(url):
        requested.append(url)
        if url not in served:
            raise urllib.error.HTTPError(url, 404, "", Message(), None)
        return _Response(served[url])

    monkeypatch.setattr("lc_reconcile._net.open_url", open_url)
    db = tmp_path / "lc.sqlite"
    fetch(base + packed.name, db)
    assert db.read_bytes() == index_path.read_bytes()
    assert len(requested) == 1 + len(_manifest(packed)["parts"])
    with pytest.raises(PrebuiltError, match="no prebuilt index"):
        fetch(base + "index-v0.json", db)


def test_cli(index_path, tmp_path, capsys):
    out = tmp_path / "out"
    assert cli.main(["--db", str(index_path), "pack-index", str(out)]) == 0
    db = tmp_path / "lc.sqlite"
    assert cli.main(["--db", str(db), "fetch-index", str(out / manifest_name())]) == 0
    captured = capsys.readouterr()
    assert "names: 4 records" in captured.out
    assert "lc-reconcile sync" in captured.err
