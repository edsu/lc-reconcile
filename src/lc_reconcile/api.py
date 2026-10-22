"""The reconciliation web service: Reconciliation Service API 0.2 over HTTP.

https://www.w3.org/community/reports/reconciliation/CG-FINAL-specs-0.2-20230410/

The reconciliation endpoint is ``/reconcile``, the URL a client such as
OpenRefine is given. ``GET`` with no ``queries`` returns the service
manifest; ``GET`` or ``POST`` with ``queries`` (a JSON batch, form-encoded
for a POST) returns results keyed by query id. Either may ask for JSONP with
``callback``, which older OpenRefine uses.

The manifest also points to the optional services, under the endpoint:

- ``GET /reconcile/suggest/entity``, ``…/suggest/type`` and
  ``…/suggest/property``, with ``prefix`` (and for entities ``type`` and
  ``cursor``), for autocomplete
- ``GET /reconcile/preview?id=…``, a small HTML card for one heading

``GET /`` is a page for trying the service in a browser, and ``GET /status``
says what the index holds.

Needs the ``server`` extra: ``pip install 'lc-reconcile[server]'``.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from lc_reconcile.match import TYPES, Reconciler
from lc_reconcile.records import AuthorityRecord
from lc_reconcile.store import Index

#: The reconciliation endpoint, under which the suggest and preview services
#: are too.
ENDPOINT = "/reconcile"

#: The most candidates one query may ask for.
MAX_LIMIT = 100

#: A JSONP callback must be a plain JavaScript name, so that a request can't
#: make the service return a script of its choosing.
_CALLBACK = re.compile(r"^[A-Za-z_$][\w$.]{0,63}$")


#: Query properties the service understands (see lc_reconcile.match.Query).
PROPERTIES = (
    {
        "id": "marc_key",
        "name": "marc_key",
        "description": "the heading as a MARC field, e.g. 7001 $aVerne, Jules,$d1828-1905.",
    },
    {
        "id": "dates",
        "name": "dates",
        "description": "dates to add to a name heading, e.g. 1828-1905",
    },
    {
        "id": "scheme",
        "name": "scheme",
        "description": "LC schemes to match against, separated by spaces",
    },
)

#: The size OpenRefine gives a preview's frame, in pixels.
PREVIEW_WIDTH, PREVIEW_HEIGHT = 400, 160


def manifest(base: str) -> dict[str, Any]:
    """The service manifest, for a service whose root is at ``base``."""
    base = base.rstrip("/") + ENDPOINT
    return {
        "versions": ["0.2"],
        "name": "Library of Congress authorities (lc-reconcile)",
        "identifierSpace": "http://id.loc.gov/authorities/",
        "schemaSpace": "http://www.loc.gov/mads/rdf/v1#",
        "defaultTypes": [{"id": name, "name": name} for name in TYPES],
        "view": {"url": "{{id}}"},
        "preview": {
            "url": base + "/preview?id={{id}}",
            "width": PREVIEW_WIDTH,
            "height": PREVIEW_HEIGHT,
        },
        "suggest": {
            kind: {"service_url": base, "service_path": f"/suggest/{kind}"}
            for kind in ("entity", "type", "property")
        },
        "documentation": "https://github.com/edsu/lc-reconcile",
        "batchSize": 50,
    }


class _Reconcilers:
    """One reconciler, and so one SQLite connection, per worker thread."""

    def __init__(self, db: Path) -> None:
        self.db = db
        self._local = threading.local()
        self._lock = threading.Lock()
        self._indexes: list[Index] = []

    def get(self) -> Reconciler:
        reconciler = getattr(self._local, "reconciler", None)
        if reconciler is None:
            index = Index(self.db)
            with self._lock:
                self._indexes.append(index)
            reconciler = self._local.reconciler = Reconciler(index)
        return reconciler

    def close(self) -> None:
        with self._lock:
            for index in self._indexes:
                index.close()
            self._indexes.clear()


class BadRequest(ValueError):
    """A request the service can't make sense of."""


#: How a worker process started by ``serve --workers`` learns its index.
DB_ENV = "LC_RECONCILE_DB"


def app_from_environment() -> FastAPI:
    """The service for the index named by ``$LC_RECONCILE_DB``.

    For uvicorn to call in each worker process, which can't be handed an
    app object.
    """
    return create_app(os.environ[DB_ENV])


def create_app(db: str | Path) -> FastAPI:
    """The service for the index at ``db``, which must already exist."""
    db = Path(db)
    Index(db).close()  # fail now, not on the first query, if it's missing
    reconcilers = _Reconcilers(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        yield
        reconcilers.close()

    app = FastAPI(
        title="lc-reconcile",
        summary="Reconcile headings against a local index of id.loc.gov",
        lifespan=lifespan,
    )
    # Reconciliation clients include web pages served from other origins.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"])

    @app.api_route(ENDPOINT, methods=["GET", "POST"])
    async def reconcile(request: Request) -> Response:
        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            params.update((await request.form()).items())
        callback = params.get("callback")
        if callback is not None and not _CALLBACK.match(str(callback)):
            return _error("callback must be a JavaScript name", None)

        queries = params.get("queries")
        if queries is None:
            return _respond(manifest(str(request.base_url)), callback)
        try:
            batch = _parse_batch(str(queries))
            # In the worker thread, so that it uses that thread's connection.
            body = await run_in_threadpool(lambda: reconcilers.get().match(batch))
        except ValueError as error:  # BadRequest, or a query of an unknown type
            return _error(str(error), callback)
        return _respond(body, callback)

    # Plain functions, which FastAPI runs in a worker thread.
    @app.get(ENDPOINT + "/suggest/entity")
    def suggest_entity(
        prefix: str = "",
        type_: str | None = Query(None, alias="type"),
        cursor: int = 0,
        callback: str | None = None,
    ) -> Response:
        if callback is not None and not _CALLBACK.match(callback):
            return _error("callback must be a JavaScript name", None)
        if type_ is not None and type_ not in TYPES:
            return _error(f"unknown type {type_!r}", callback)
        results = reconcilers.get().suggest(
            prefix, types=TYPES[type_] if type_ else None, offset=max(cursor, 0)
        )
        return _respond({"result": results}, callback)

    @app.get(ENDPOINT + "/suggest/type")
    def suggest_type(prefix: str = "", callback: str | None = None) -> Response:
        found = [
            {"id": name, "name": name}
            for name in TYPES
            if prefix.strip().lower() in name.lower()
        ]
        return _suggestions(found, callback)

    @app.get(ENDPOINT + "/suggest/property")
    def suggest_property(prefix: str = "", callback: str | None = None) -> Response:
        found = [p for p in PROPERTIES if prefix.strip().lower() in p["id"]]
        return _suggestions(found, callback)

    @app.get(ENDPOINT + "/preview", response_class=HTMLResponse)
    def preview(uri: str = Query(alias="id")) -> HTMLResponse:
        index = reconcilers.get().index
        record = index.get(uri)
        if record is None:
            return HTMLResponse(_page("<p>No such heading.</p>"), status_code=404)
        replacements = [index.get(target) for target in record.use_instead]
        return HTMLResponse(_page(_card(record, replacements)))

    @app.get("/", response_class=HTMLResponse)
    def demo() -> HTMLResponse:
        return HTMLResponse(_DEMO)

    @app.get("/status")
    def status() -> dict[str, Any]:
        with Index(db) as index:
            return {"schemes": [asdict(s) for s in index.schemes()]}

    return app


_DEMO = files("lc_reconcile").joinpath("demo.html").read_text(encoding="utf-8")


def _suggestions(found: list[dict[str, Any]], callback: str | None) -> Response:
    if callback is not None and not _CALLBACK.match(callback):
        return _error("callback must be a JavaScript name", None)
    return _respond({"result": found}, callback)


_PREVIEW_STYLE = """
body { font: 13px/1.4 system-ui, sans-serif; margin: 8px; color: #222; background: #fff; }
h1 { font-size: 15px; margin: 0 0 4px; }
p { margin: 2px 0; }
.note { color: #555; }
.warn { color: #a40; }
a { color: #05a; }
"""


def _page(body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<style>{_PREVIEW_STYLE}</style></head><body>{body}</body></html>"
    )


def _card(record: AuthorityRecord, replacements: list[AuthorityRecord | None]) -> str:
    """A heading's preview, as HTML. Every value is escaped: it's LC's data."""
    e = html.escape
    uri = e(record.uri)
    lines = [
        f'<h1><a href="{uri}" target="_blank">{e(record.label or record.uri)}</a></h1>'
    ]
    about = ", ".join(filter(None, (record.kind, record.scheme)))
    if record.lccn:
        about += f" · LCCN {record.lccn}"
    lines.append(f'<p class="note">{e(about)}</p>')
    if record.birth or record.death:
        lines.append(f"<p>{e(record.birth or '')} – {e(record.death or '')}</p>")
    if "collection_NamesUndifferentiated" in record.collections:
        lines.append(
            '<p class="warn">Undifferentiated: stands for more than one person.</p>'
        )
    if record.deprecated:
        lines.append('<p class="warn">Deprecated.</p>')
        if record.deletion_note:
            lines.append(f'<p class="note">{e(record.deletion_note)}</p>')
        for replacement in filter(None, replacements):
            lines.append(
                f'<p>Use instead: <a href="{e(replacement.uri)}" target="_blank">'
                f"{e(replacement.label or replacement.uri)}</a></p>"
            )
    if record.rwo:
        lines.append(f'<p class="note">RWO: {e(record.rwo)}</p>')
    return "".join(lines)


def _parse_batch(text: str) -> dict[str, dict[str, Any]]:
    try:
        batch = json.loads(text)
    except json.JSONDecodeError as error:
        raise BadRequest(f"queries is not JSON: {error}") from None
    if not isinstance(batch, dict) or not all(
        isinstance(query, dict) for query in batch.values()
    ):
        raise BadRequest('queries must be an object of queries, {"q0": {...}}')
    for query in batch.values():
        if "limit" in query:
            try:
                query["limit"] = max(1, min(int(query["limit"]), MAX_LIMIT))
            except (TypeError, ValueError):
                raise BadRequest("limit must be a number") from None
    return batch


def _respond(body: Any, callback: str | None) -> Response:
    if callback:
        script = f"{callback}({json.dumps(body, ensure_ascii=False)});"
        return Response(script, media_type="application/javascript")
    return JSONResponse(body)


def _error(message: str, callback: str | None) -> Response:
    # A JSONP caller can't see a status code, only the script it gets.
    if callback:
        return _respond({"error": message}, callback)
    return JSONResponse({"error": message}, status_code=400)
