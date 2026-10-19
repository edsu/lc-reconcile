"""Answer reconciliation queries from the index.

Queries and results have the shape of the Reconciliation Service API 0.2
(https://www.w3.org/community/reports/reconciliation/CG-FINAL-specs-0.2-20230410/),
so the web service passes them straight through and a library caller can
build them the same way.

Only an exact match on an authorized heading counts as ``match: true``, and
only when it is the one such heading of the requested type. Everything else
a lookup finds -- variants, several authorized headings, deprecated ones --
comes back as candidates for a person to choose between.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from lc_reconcile.normalize import normalize
from lc_reconcile.records import AuthorityRecord
from lc_reconcile.store import Hit, Index

#: Reconciliation types this service offers, and the MADS kinds each accepts.
#: A subdivided heading is always a ComplexSubject in MADS, whatever its first
#: element, so a query containing ``--`` accepts that kind too.
TYPES: dict[str, frozenset[str]] = {
    "PersonalName": frozenset({"PersonalName"}),
    "CorporateName": frozenset({"CorporateName"}),
    "ConferenceName": frozenset({"ConferenceName"}),
    "FamilyName": frozenset({"FamilyName"}),
    "NameTitle": frozenset({"NameTitle"}),
    "Title": frozenset({"Title"}),
    "Geographic": frozenset({"Geographic", "HierarchicalGeographic"}),
    "Topic": frozenset({"Topic"}),
    "ComplexSubject": frozenset({"ComplexSubject"}),
    "GenreForm": frozenset({"GenreForm"}),
    "Temporal": frozenset({"Temporal"}),
}

#: Scores for each way a candidate can be found. They order the candidates;
#: only ``match`` says whether one may be accepted without review.
SCORE_AUTHORIZED = 100
SCORE_VARIANT = 90
SCORE_REPLACEMENT = 70

#: Records in this collection stand for more than one person.
UNDIFFERENTIATED = "collection_NamesUndifferentiated"
#: Free-floating and other subdivisions, which are not headings on their own.
SUBDIVISIONS = "collection_Subdivisions"

DEFAULT_LIMIT = 10


@dataclass(slots=True)
class Query:
    text: str
    types: frozenset[str] | None = None
    schemes: frozenset[str] | None = None
    dates: str | None = None
    limit: int = DEFAULT_LIMIT

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Query:
        """A query in the 0.2 shape: query, type, limit and properties."""
        text = str(data.get("query", ""))
        kinds = _kinds(data.get("type"))
        properties: dict[str, str] = {}
        for prop in data.get("properties") or []:
            pid, value = prop.get("pid"), prop.get("v")
            if isinstance(value, dict):  # an already-reconciled value
                value = value.get("id") or value.get("name")
            if pid and value is not None:
                properties[str(pid)] = str(value)
        schemes = properties.get("scheme")
        return cls(
            text=text,
            types=kinds,
            schemes=frozenset(schemes.split()) if schemes else None,
            dates=properties.get("dates"),
            limit=int(data.get("limit") or DEFAULT_LIMIT),
        )


def _kinds(type_: Any) -> frozenset[str] | None:
    names = [type_] if isinstance(type_, str) else list(type_ or [])
    kinds: set[str] = set()
    for name in names:
        if name not in TYPES:
            raise ValueError(f"unknown type {name!r}; expected one of {sorted(TYPES)}")
        kinds |= TYPES[name]
    return frozenset(kinds) or None


@dataclass(slots=True)
class Candidate:
    record: AuthorityRecord
    score: int
    match: bool
    #: How the candidate was found, for a person reviewing it.
    reason: str

    def to_json(self) -> dict[str, Any]:
        record = self.record
        result: dict[str, Any] = {
            "id": record.uri,
            "name": record.label or (record.uri.rsplit("/", 1)[-1]),
            "type": [{"id": record.kind, "name": record.kind}] if record.kind else [],
            "score": self.score,
            "match": self.match,
            "description": self.reason,
        }
        if record.rwo:
            result["rwo"] = record.rwo
        return result


def _with_dates(query: Query) -> str:
    """The query text, with its dates appended if it doesn't carry them."""
    if not query.dates or normalize(query.dates) in normalize(query.text):
        return query.text
    return f"{query.text.rstrip(' ,.')}, {query.dates}"


class Reconciler:
    """Match headings against an :class:`~lc_reconcile.store.Index`."""

    def __init__(self, index: Index, *, accept_unique_variant: bool = False) -> None:
        self.index = index
        #: Count a unique variant-label hit as a match. Off by default: a
        #: variant can be shared by unrelated headings, and LC's own label
        #: service treats variants this loosely, which is what we avoid.
        self.accept_unique_variant = accept_unique_variant

    def match(self, queries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """Answer a 0.2 batch: ``{"q0": {...}, ...}`` → ``{"q0": {"result": [...]}}``."""
        return {
            key: {"result": [c.to_json() for c in self.candidates(Query.from_json(q))]}
            for key, q in queries.items()
        }

    def candidates(self, query: Query) -> list[Candidate]:
        key = normalize(_with_dates(query))
        if not key:
            return []
        if query.types and "--" in key:
            query = replace(query, types=query.types | {"ComplexSubject"})
        hits = [h for h in self.index.lookup(key) if _wanted(h.record, query)]

        authorized = _prefer_headings([h for h in hits if h.via == "authorized"])
        if authorized:
            unique = len(authorized) == 1
            return [
                Candidate(
                    hit.record,
                    SCORE_AUTHORIZED,
                    unique and UNDIFFERENTIATED not in hit.record.collections,
                    _authorized_reason(hit.record, unique),
                )
                for hit in authorized
            ][: query.limit]

        variants = [h for h in hits if h.via == "variant"]
        if variants:
            unique = len(variants) == 1
            return [
                Candidate(
                    hit.record,
                    SCORE_VARIANT,
                    unique and self.accept_unique_variant,
                    "matches a variant of this heading",
                )
                for hit in variants
            ][: query.limit]

        # Only deprecated headings are left: offer what LC says replaces them.
        candidates: dict[str, Candidate] = {}
        for hit in hits:
            note = hit.record.deletion_note
            for target in self.index.use_instead(hit.record.uri):
                replacement = self.index.get(target)
                if (
                    replacement is None
                    or replacement.deprecated
                    or replacement.uri in candidates
                    or not _wanted(replacement, query)
                ):
                    continue
                candidates[replacement.uri] = Candidate(
                    replacement,
                    SCORE_REPLACEMENT,
                    False,
                    f"replaces the deprecated heading {hit.record.uri}"
                    + (f": {note}" if note else ""),
                )
        return list(candidates.values())[: query.limit]


def _wanted(record: AuthorityRecord, query: Query) -> bool:
    if query.schemes and record.scheme not in query.schemes:
        return False
    return not query.types or record.kind in query.types


def _prefer_headings(hits: list[Hit]) -> list[Hit]:
    """Drop subdivision records when a full heading has the same label.

    "Fiction" is both an LCSH heading and a form subdivision (``$v``);
    someone reconciling a heading means the former.
    """
    headings = [h for h in hits if SUBDIVISIONS not in h.record.collections]
    return headings or hits


def _authorized_reason(record: AuthorityRecord, unique: bool) -> str:
    if UNDIFFERENTIATED in record.collections:
        return "matches an undifferentiated name, which covers more than one person"
    if not unique:
        return "matches one of several authorized headings with this label"
    return "matches the authorized heading"
