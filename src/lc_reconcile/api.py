"""The reconciliation web service: Reconciliation Service API 0.2 over HTTP.

https://www.w3.org/community/reports/reconciliation/CG-FINAL-specs-0.2-20230410/

``GET /`` with no ``queries`` returns the service manifest; ``GET`` or
``POST /`` with ``queries`` (a JSON batch, form-encoded for a POST) returns
results keyed by query id. Either may ask for JSONP with ``callback``, which
older OpenRefine uses. ``GET /status`` says what the index holds.

Needs the ``server`` extra: ``pip install 'lc-reconcile[server]'``.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from lc_reconcile.match import TYPES, Reconciler
from lc_reconcile.store import Index

#: The most candidates one query may ask for.
MAX_LIMIT = 100

#: A JSONP callback must be a plain JavaScript name, so that a request can't
#: make the service return a script of its choosing.
_CALLBACK = re.compile(r"^[A-Za-z_$][\w$.]{0,63}$")


def manifest() -> dict[str, Any]:
    return {
        "versions": ["0.2"],
        "name": "Library of Congress authorities (lc-reconcile)",
        "identifierSpace": "http://id.loc.gov/authorities/",
        "schemaSpace": "http://www.loc.gov/mads/rdf/v1#",
        "defaultTypes": [{"id": name, "name": name} for name in TYPES],
        "view": {"url": "{{id}}"},
        "documentation": "https://github.com/blue-core-lod/lc-reconcile",
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

    @app.api_route("/", methods=["GET", "POST"])
    async def reconcile(request: Request) -> Response:
        params: dict[str, Any] = dict(request.query_params)
        if request.method == "POST":
            params.update((await request.form()).items())
        callback = params.get("callback")
        if callback is not None and not _CALLBACK.match(str(callback)):
            return _error("callback must be a JavaScript name", None)

        queries = params.get("queries")
        if queries is None:
            return _respond(manifest(), callback)
        try:
            batch = _parse_batch(str(queries))
            body = await run_in_threadpool(reconcilers.get().match, batch)
        except ValueError as error:  # BadRequest, or a query of an unknown type
            return _error(str(error), callback)
        return _respond(body, callback)

    @app.get("/status")
    def status() -> dict[str, Any]:
        with Index(db) as index:
            return {"schemes": [asdict(s) for s in index.schemes()]}

    return app


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
