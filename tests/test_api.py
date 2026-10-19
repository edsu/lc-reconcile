"""The web service, through FastAPI's test client."""

import json
import urllib.request

import jsonschema
import pytest
from fastapi.testclient import TestClient
from referencing import Registry
from referencing.jsonschema import DRAFT7

from lc_reconcile import IndexUnavailable
from lc_reconcile.api import create_app

from .conftest import VERNE

SCIENCE_FICTION = "http://id.loc.gov/authorities/subjects/sh85118629"


@pytest.fixture
def client(index_path):
    # As a context manager, so the app's shutdown closes its connections.
    with TestClient(create_app(index_path)) as client:
        yield client


def _queries(**queries):
    return {"queries": json.dumps(queries)}


def test_manifest(client):
    response = client.get("/")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["versions"] == ["0.2"]
    assert manifest["identifierSpace"] == "http://id.loc.gov/authorities/"
    assert {"id": "PersonalName", "name": "PersonalName"} in manifest["defaultTypes"]
    assert manifest["view"] == {"url": "{{id}}"}


def test_queries_by_get(client):
    response = client.get(
        "/", params=_queries(q0={"query": "Science fiction", "type": "Topic"})
    )
    assert response.status_code == 200
    [candidate] = response.json()["q0"]["result"]
    assert candidate["id"] == SCIENCE_FICTION
    assert candidate["match"] is True


def test_queries_by_post(client):
    # OpenRefine posts the batch form-encoded.
    batch = _queries(
        q0={
            "query": "Verne, Jules",
            "type": "PersonalName",
            "properties": [{"pid": "dates", "v": "1828-1905"}],
        },
        q1={
            "query": "",
            "properties": [
                {"pid": "marc_key", "v": "7001 $aVerne, Jules,$d1828-1905."}
            ],
        },
        q2={"query": "No such heading anywhere"},
    )
    response = client.post("/", data=batch)
    assert response.status_code == 200
    results = response.json()
    assert results["q0"]["result"][0]["id"] == VERNE
    assert results["q0"]["result"][0]["rwo"] == "http://id.loc.gov/rwo/agents/n79064013"
    assert results["q1"]["result"][0]["match"] is True
    assert results["q2"] == {"result": []}


def test_jsonp(client):
    response = client.get("/", params={"callback": "jQuery_1.done"})
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.text.startswith("jQuery_1.done({")
    assert response.text.endswith("});")


def test_jsonp_callback_must_be_a_name(client):
    response = client.get("/", params={"callback": "alert(1)//"})
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("queries", "message"),
    [
        ("{not json", "not JSON"),
        ('["Science fiction"]', "object of queries"),
        (json.dumps({"q0": {"query": "x", "type": "Spaceship"}}), "unknown type"),
        (json.dumps({"q0": {"query": "x", "limit": "lots"}}), "limit"),
    ],
)
def test_bad_queries(client, queries, message):
    response = client.get("/", params={"queries": queries})
    assert response.status_code == 400
    assert message in response.json()["error"]


def test_limit_is_capped(client):
    batch = _queries(q0={"query": "Verne, Julio, 1828-1905", "limit": 100_000})
    assert client.get("/", params=batch).status_code == 200


def test_cors(client):
    response = client.get("/", headers={"Origin": "https://example.org"})
    assert response.headers["access-control-allow-origin"] == "*"


def test_status(client):
    schemes = {s["name"]: s for s in client.get("/status").json()["schemes"]}
    assert schemes["subjects"]["records"] == 10
    assert schemes["subjects"]["snapshot_modified"] == "2026-10-14T00:00:00+00:00"
    assert schemes["names"]["cursor"] is None


def test_no_index(tmp_path):
    with pytest.raises(IndexUnavailable):
        create_app(tmp_path / "none.sqlite")


SCHEMAS = "https://reconciliation-api.github.io/specs/0.2/schemas/"


def _fetch_schema(uri):
    # Only the spec's own schemas, which refer to one another.
    assert uri.startswith(SCHEMAS), uri
    with urllib.request.urlopen(uri, timeout=30) as response:
        # They name the generic "http://json-schema.org/schema#" as their
        # dialect, which referencing doesn't know; they are draft 7.
        return DRAFT7.create_resource(json.load(response))


@pytest.mark.network
@pytest.mark.parametrize(
    ("schema", "params"),
    [
        ("manifest.json", {}),
        (
            "reconciliation-result-batch.json",
            _queries(
                q0={"query": "Science fiction", "type": "Topic"},
                q1={"query": "Verne, Jules"},
                q2={"query": "No such heading anywhere"},
            ),
        ),
    ],
)
def test_responses_follow_the_spec(client, schema, params):
    # The spec's own schemas, fetched rather than copied: they carry no license.
    registry = Registry(retrieve=_fetch_schema)
    contents = registry.get_or_retrieve(SCHEMAS + schema).value.contents
    validator = jsonschema.Draft7Validator(contents, registry=registry)
    body = client.get("/", params=params).json()
    assert [error.message for error in validator.iter_errors(body)] == []
