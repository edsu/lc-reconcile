import gzip
import io
import json
import urllib.request

import pytest

from lc_reconcile import Index, IndexUnavailable, Source, build
from lc_reconcile.cli import main

from .conftest import FIXTURES


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.headers = headers


def test_build_from_files(tmp_path):
    path = tmp_path / "lc.sqlite"
    build(path, [Source("subjects", str(FIXTURES / "subjects.nt"), "2026-10-14")])
    [status] = Index(path).schemes()
    assert status.name == "subjects"
    assert status.records == 10
    assert status.snapshot_modified == "2026-10-14"


def test_build_reads_gzip(tmp_path):
    gz = tmp_path / "genreForms.nt.gz"
    gz.write_bytes(gzip.compress((FIXTURES / "genreForms.nt").read_bytes()))
    build(tmp_path / "lc.sqlite", [Source("genreForms", str(gz))])
    [status] = Index(tmp_path / "lc.sqlite").schemes()
    assert status.records == 6


def test_build_fetches_the_snapshot(tmp_path, monkeypatch):
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        body = gzip.compress((FIXTURES / "genreForms.nt").read_bytes())
        return _Response(body, {"Last-Modified": "Wed, 14 Oct 2026 02:04:56 GMT"})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert (
        main(["--db", str(tmp_path / "lc.sqlite"), "build", "--scheme", "genreForms"])
        == 0
    )

    [request] = requests
    assert request.full_url == (
        "https://id.loc.gov/download/authorities/genreForms.madsrdf.nt.gz"
    )
    assert request.get_header("User-agent").startswith("lc-reconcile/")
    [status] = Index(tmp_path / "lc.sqlite").schemes()
    assert status.snapshot_modified == "2026-10-14T02:04:56+00:00"


def test_failed_build_keeps_the_old_index(tmp_path):
    path = tmp_path / "lc.sqlite"
    build(path, [Source("subjects", str(FIXTURES / "subjects.nt"))])
    with pytest.raises(FileNotFoundError):
        build(path, [Source("genreForms", str(tmp_path / "missing.nt"))])
    assert [s.name for s in Index(path).schemes()] == ["subjects"]
    assert not (tmp_path / "lc.sqlite.new").exists()


def test_missing_index(tmp_path):
    with pytest.raises(IndexUnavailable, match="lc-reconcile build"):
        Index(tmp_path / "nothing.sqlite")


def test_cli_match(index_path, capsys):
    assert (
        main(["--db", str(index_path), "match", "Verne, Jules", "--dates", "1828-1905"])
        == 0
    )
    [result] = json.loads(capsys.readouterr().out)
    assert result["id"] == "http://id.loc.gov/authorities/names/n79064013"
    assert result["match"] is True


def test_cli_status(index_path, capsys):
    assert main(["--db", str(index_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "subjects: 10 records" in out
    assert "names: 4 records" in out


def test_cli_without_an_index(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "none.sqlite"), "status"]) == 1
    assert "lc-reconcile build" in capsys.readouterr().err


def test_cli_builds_from_files(tmp_path):
    db = str(tmp_path / "lc.sqlite")
    argv = ["--db", db, "build", "--snapshot-date", "2026-10-14"]
    for scheme in ("subjects", "genreForms"):
        argv += ["--from", str(FIXTURES / f"{scheme}.nt")]
    assert main(argv) == 0
    schemes = {s.name: s for s in Index(db).schemes()}
    assert {name: s.records for name, s in schemes.items()} == {
        "genreForms": 6,
        "subjects": 10,
    }
    assert schemes["subjects"].snapshot_modified == "2026-10-14"


def test_cli_snapshot_date_for_each_file(tmp_path, capsys):
    db = str(tmp_path / "lc.sqlite")
    argv = ["--db", db, "build"]
    for scheme, date in (("subjects", "2026-10-14"), ("genreForms", "2026-10-15")):
        argv += ["--from", str(FIXTURES / f"{scheme}.nt"), "--snapshot-date", date]
    assert main(argv) == 0
    schemes = {s.name: s.snapshot_modified for s in Index(db).schemes()}
    assert schemes == {"subjects": "2026-10-14", "genreForms": "2026-10-15"}

    argv += ["--from", str(FIXTURES / "names.nt")]
    assert main(argv) == 2
    assert "one for each --from" in capsys.readouterr().err


def test_cli_from_or_scheme(tmp_path, capsys):
    argv = ["--db", str(tmp_path / "x"), "build", "--scheme", "names"]
    assert main([*argv, "--from", "f.nt"]) == 2
    assert "not both" in capsys.readouterr().err


def test_build_refuses_the_wrong_scheme(tmp_path):
    source = Source("subjects", str(FIXTURES / "genreForms.nt"))
    with pytest.raises(ValueError, match="holds genreForms records, not subjects"):
        build(tmp_path / "lc.sqlite", [source])
    assert not (tmp_path / "lc.sqlite").exists()
