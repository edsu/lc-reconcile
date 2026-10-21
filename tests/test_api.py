"""The web service, through FastAPI's test client."""

import json
import urllib.request

import jsonschema
import pytest
from fastapi.testclient import TestClient
from referencing import Registry
from referencing.jsonschema import DRAFT7

from lc_reconcile import IndexUnavailable
from lc_reconcile.api import _card, create_app
from lc_reconcile.records import AuthorityRecord

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
    response = client.get("/reconcile")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["versions"] == ["0.2"]
    assert manifest["identifierSpace"] == "http://id.loc.gov/authorities/"
    assert {"id": "PersonalName", "name": "PersonalName"} in manifest["defaultTypes"]
    assert manifest["view"] == {"url": "{{id}}"}


def test_queries_by_get(client):
    response = client.get(
        "/reconcile", params=_queries(q0={"query": "Science fiction", "type": "Topic"})
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
    response = client.post("/reconcile", data=batch)
    assert response.status_code == 200
    results = response.json()
    assert results["q0"]["result"][0]["id"] == VERNE
    assert results["q0"]["result"][0]["rwo"] == "http://id.loc.gov/rwo/agents/n79064013"
    assert results["q1"]["result"][0]["match"] is True
    assert results["q2"] == {"result": []}


def test_jsonp(client):
    response = client.get("/reconcile", params={"callback": "jQuery_1.done"})
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.text.startswith("jQuery_1.done({")
    assert response.text.endswith("});")


def test_jsonp_callback_must_be_a_name(client):
    response = client.get("/reconcile", params={"callback": "alert(1)//"})
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
    response = client.get("/reconcile", params={"queries": queries})
    assert response.status_code == 400
    assert message in response.json()["error"]


def test_limit_is_capped(client):
    batch = _queries(q0={"query": "Verne, Julio, 1828-1905", "limit": 100_000})
    assert client.get("/reconcile", params=batch).status_code == 200


def test_cors(client):
    response = client.get("/reconcile", headers={"Origin": "https://example.org"})
    assert response.headers["access-control-allow-origin"] == "*"


def test_status(client):
    schemes = {s["name"]: s for s in client.get("/status").json()["schemes"]}
    assert schemes["subjects"]["records"] == 10
    assert schemes["subjects"]["snapshot_modified"] == "2026-10-14T00:00:00+00:00"
    assert schemes["names"]["cursor"] is None


def test_manifest_points_to_suggest_and_preview(client):
    manifest = client.get("/reconcile").json()
    assert manifest["preview"]["url"] == "http://testserver/reconcile/preview?id={{id}}"
    entity = manifest["suggest"]["entity"]
    assert (entity["service_url"], entity["service_path"]) == (
        "http://testserver/reconcile",
        "/suggest/entity",
    )


def _suggest(client, prefix, **params):
    response = client.get(
        "/reconcile/suggest/entity", params={"prefix": prefix, **params}
    )
    assert response.status_code == 200
    return response.json()["result"]


def test_suggest_entities(client):
    [verne] = _suggest(client, "verne, j")
    assert verne["id"] == VERNE
    assert verne["name"] == "Verne, Jules, 1828-1905"
    assert verne["notable"] == [{"id": "PersonalName", "name": "PersonalName"}]


def test_suggest_by_variant_says_so(client):
    [verne] = _suggest(client, "Verne, Juli")
    assert verne["id"] == VERNE
    assert "variant form “verne, julio" in verne["description"]


def test_suggest_puts_headings_first(client):
    names = [s["name"] for s in _suggest(client, "science", type="Topic")]
    assert names[0] == "Science fiction"


def test_suggest_needs_two_characters(client):
    assert _suggest(client, "v") == []


def test_suggest_by_type(client):
    assert _suggest(client, "science f", type="PersonalName") == []
    response = client.get(
        "/reconcile/suggest/entity", params={"prefix": "x", "type": "Nope"}
    )
    assert response.status_code == 400


def test_suggest_cursor(client):
    every = _suggest(client, "fi")
    assert len(every) > 1
    assert _suggest(client, "fi", cursor=1) == every[1:]


def test_suggest_jsonp(client):
    response = client.get(
        "/reconcile/suggest/type", params={"prefix": "geo", "callback": "cb"}
    )
    assert (
        response.text == 'cb({"result": [{"id": "Geographic", "name": "Geographic"}]});'
    )


def test_suggest_types_and_properties(client):
    types = client.get("/reconcile/suggest/type", params={"prefix": "name"}).json()[
        "result"
    ]
    assert {t["id"] for t in types} == {
        "PersonalName",
        "CorporateName",
        "ConferenceName",
        "FamilyName",
        "NameTitle",
    }
    properties = client.get(
        "/reconcile/suggest/property", params={"prefix": "mar"}
    ).json()
    assert [p["id"] for p in properties["result"]] == ["marc_key"]


def test_preview(client):
    response = client.get("/reconcile/preview", params={"id": VERNE})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Verne, Jules, 1828-1905" in response.text
    assert "PersonalName, names" in response.text


def test_preview_of_a_deprecated_heading(client):
    godzilla_films = "http://id.loc.gov/authorities/genreForms/gf2011026302"
    page = client.get("/reconcile/preview", params={"id": godzilla_films}).text
    assert "Deprecated" in page
    assert "Use instead" in page
    assert "Monster films" in page


def test_preview_escapes():
    card = _card(AuthorityRecord(uri="http://id.loc.gov/x", label="<script>"), [])
    assert "<script>" not in card
    assert "&lt;script&gt;" in card


def test_preview_of_nothing(client):
    response = client.get(
        "/reconcile/preview", params={"id": "http://id.loc.gov/nothing"}
    )
    assert response.status_code == 404


def test_demo_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "suggest/entity" in response.text


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
    ("schema", "path", "params"),
    [
        ("manifest.json", "/reconcile", {}),
        (
            "reconciliation-result-batch.json",
            "/reconcile",
            _queries(
                q0={"query": "Science fiction", "type": "Topic"},
                q1={"query": "Verne, Jules"},
                q2={"query": "No such heading anywhere"},
            ),
        ),
        (
            "suggest-entities-response.json",
            "/reconcile/suggest/entity",
            {"prefix": "verne"},
        ),
        ("suggest-types-response.json", "/reconcile/suggest/type", {"prefix": "name"}),
        (
            "suggest-properties-response.json",
            "/reconcile/suggest/property",
            {"prefix": ""},
        ),
    ],
)
def test_responses_follow_the_spec(client, schema, path, params):
    # The spec's own schemas, fetched rather than copied: they carry no license.
    registry = Registry(retrieve=_fetch_schema)
    contents = registry.get_or_retrieve(SCHEMAS + schema).value.contents
    validator = jsonschema.Draft7Validator(contents, registry=registry)
    body = client.get(path, params=params).json()
    assert [error.message for error in validator.iter_errors(body)] == []
