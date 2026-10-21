"""Answer reconciliation queries from the index.

Queries and results have the shape of the Reconciliation Service API 0.2
(https://www.w3.org/community/reports/reconciliation/CG-FINAL-specs-0.2-20230410/),
so the web service passes them straight through and a library caller can
build them the same way.

A query can give the heading as a marcKey (the ``marc_key`` property) as well
as, or instead of, a label. The marcKey is tried first, compared subfield by
subfield; the label is tried when it finds nothing.

Only an exact match on an authorized heading counts as ``match: true``, and
only when it is the one such heading of the requested type -- or, failing
that, on the one heading that has the query as an earlier established form,
the heading LC replaced it with. Everything else a lookup finds -- other
variants, several authorized headings, deprecated ones -- comes back as
candidates for a person to choose between, and when it finds nothing at
all, headings with similar labels do. A field that says its heading is from
another vocabulary (``650 2``, ``$2fast``) gets the same candidates, but
never a match.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Any

from lc_reconcile.marckey import (
    from_lc,
    heading_key,
    is_subdivided,
    kinds,
    label_from_marc_key,
    schemes,
)
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
    "Medium": frozenset({"Medium"}),
    "Temporal": frozenset({"Temporal"}),
}

#: The schemes a query is matched against when it doesn't say which. LC's
#: other vocabularies repeat many of their headings -- children's subjects
#: has its own "Science fiction", LCDGT its own "Americans" -- so a query
#: reaches them only by asking: with the ``scheme`` property, with a marcKey
#: whose indicator or ``$2`` names one, or with a type only one of them has.
GENERAL_SCHEMES = frozenset({"names", "subjects", "genreForms"})

#: Kinds of heading that only one scheme has.
_SCHEME_OF_KIND = {"Medium": "performanceMediums"}

#: Scores for each way a candidate can be found. They order the candidates;
#: only ``match`` says whether one may be accepted without review.
SCORE_AUTHORIZED = 100
SCORE_EARLIER = 95
SCORE_VARIANT = 90
SCORE_REPLACEMENT = 70
#: A similar label scores up to this, by how alike the two are, ...
SCORE_SIMILAR = 55
#: ... and this much more when the dates asked for are the heading's, or
#: this much less when they aren't.
SCORE_DATES = 10
#: How alike (see :func:`similarity`) a label must be to be offered at all.
#: A search that shares only a word or two with a label finds a great deal.
SIMILAR_ENOUGH = 0.5

_YEAR = re.compile(r"\b\d{4}\b")
_NUMBERS = re.compile(r"\s*\b\d+\b")
_WORD = re.compile(r"\w+")

#: Ways a key can match one of a heading's other forms.
_VARIANTS = ("earlier", "variant")

#: Records in this collection stand for more than one person.
UNDIFFERENTIATED = "collection_NamesUndifferentiated"
#: Free-floating and other subdivisions, which are not headings on their own.
SUBDIVISIONS = "collection_Subdivisions"

DEFAULT_LIMIT = 10

#: The shortest prefix, once normalized, that autocomplete looks up.
SUGGEST_MIN = 2


@dataclass(slots=True)
class Query:
    text: str
    types: frozenset[str] | None = None
    schemes: frozenset[str] | None = None
    dates: str | None = None
    #: The heading as a MARC field, e.g. ``7001 $aVerne, Jules,$d1828-1905.``
    marc_key: str | None = None
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
            marc_key=properties.get("marc_key"),
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
        found = self._candidates(query)
        if query.marc_key and not from_lc(query.marc_key):
            for candidate in found:
                if candidate.match:
                    candidate.match = False
                    candidate.reason += ", but the field names another vocabulary"
        return found

    def _candidates(self, query: Query) -> list[Candidate]:
        keys = []
        field = query.marc_key
        marc_key = heading_key(field) if field else None
        if field and marc_key:
            keys.append(marc_key)
            if query.types is None:
                # A 710 is a corporate name whatever its label looks like.
                query = replace(query, types=kinds(field))
        if field and query.schemes is None:
            query = replace(query, schemes=schemes(field))
        if query.schemes is None:
            query = replace(query, schemes=_default_schemes(query.types))
        label_key = normalize(_with_dates(query))
        if not label_key and field:
            # The field's heading as a label too: a field that isn't a
            # heading, such as a 385 of LCDGT terms, has nothing else, and LC
            # gives earlier established forms as labels without marcKeys.
            label_key = normalize(label_from_marc_key(field))
        if label_key:
            keys.append(label_key)
        subdivided = "--" in label_key or (marc_key and is_subdivided(marc_key))
        if query.types and subdivided:
            query = replace(query, types=query.types | {"ComplexSubject"})
        found = [
            [h for h in self.index.lookup(key) if _wanted(h.record, query)]
            for key in dict.fromkeys(keys)
        ]
        # The marcKey is tried before the label: an authorized heading is
        # taken from the first key that finds one, so that a marcKey's exact
        # match isn't made ambiguous by headings that only read alike.
        authorized = next(
            (
                _prefer_headings(authorized)
                for hits in found
                if (authorized := [h for h in hits if h.via == "authorized"])
            ),
            [],
        )
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

        # A heading's earlier form is the heading LC replaced, so a record
        # catalogued before the change is matched to its successor -- if
        # nothing else has the label as a variant.
        # Other forms are pooled across the keys, each record counted once,
        # as an earlier form if either key finds one: the marcKey can find a
        # variant whose label is also an earlier form.
        pooled: dict[str, Hit] = {}
        for hits in found:
            for hit in hits:
                if hit.via in _VARIANTS:
                    current = pooled.get(hit.record.uri)
                    if current is None or current.via != "earlier":
                        pooled[hit.record.uri] = hit
        variants = _prefer_headings(list(pooled.values()))
        if variants:
            unique = len(variants) == 1
            ranked = sorted(variants, key=lambda h: h.via != "earlier")
            return [
                Candidate(
                    hit.record,
                    SCORE_EARLIER if hit.via == "earlier" else SCORE_VARIANT,
                    unique
                    and UNDIFFERENTIATED not in hit.record.collections
                    and (hit.via == "earlier" or self.accept_unique_variant),
                    "matches an earlier form of this heading"
                    if hit.via == "earlier"
                    else "matches a variant of this heading",
                )
                for hit in ranked
            ][: query.limit]

        # Only deprecated headings are left: offer what LC says replaces them.
        hits = next((hits for hits in found if hits), [])
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
        if candidates:
            return list(candidates.values())[: query.limit]

        if not label_key and query.marc_key:
            # A heading LC hasn't established, such as a subject with a
            # subdivision added, may still be like one it has.
            label_key = normalize(label_from_marc_key(query.marc_key))
        return self._similar(label_key, query) if label_key else []

    def suggest(
        self,
        prefix: str,
        *,
        types: frozenset[str] | None = None,
        schemes: frozenset[str] | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Headings starting with ``prefix``, for autocomplete.

        Results have the shape of the 0.2 suggest service. A prefix of fewer
        than :data:`SUGGEST_MIN` characters finds nothing: it would match a
        great many labels and say little about any of them.
        """
        key = normalize(prefix)
        if len(key) < SUGGEST_MIN:
            return []
        if schemes is None:
            schemes = _default_schemes(types)
        found = self.index.starting_with(
            key, kinds=types, schemes=schemes, limit=limit, offset=offset
        )
        results = []
        for hit, label_key in found:
            record = hit.record
            about = ", ".join(filter(None, (record.kind, record.scheme)))
            if hit.via != "authorized":
                about += f"; from {hit.via} form “{label_key}”"
            result: dict[str, Any] = {
                "id": record.uri,
                "name": record.label or record.uri,
                "description": about,
            }
            if record.kind:
                result["notable"] = [{"id": record.kind, "name": record.kind}]
            results.append(result)
        return results

    def _similar(self, key: str, query: Query) -> list[Candidate]:
        """Headings with labels like ``key``, for when nothing matches it."""
        best: dict[str, Candidate] = {}
        # Labels are compared without their numbers, and dates are weighed
        # on their own: "Morrison, Toni" says nothing about her dates, so the
        # heading's dates shouldn't count against it, but dates that differ
        # suggest someone else.
        asked = set(_YEAR.findall(key))
        name = _without_numbers(key)
        found = self.index.search(key, kinds=query.types, schemes=query.schemes)
        for hit, label_key in found:
            ratio = similarity(name, _without_numbers(label_key))
            if ratio < SIMILAR_ENOUGH:
                continue
            score = round(SCORE_SIMILAR * ratio)
            if asked:
                agree = asked <= _years(hit.record)
                score += SCORE_DATES if agree else -SCORE_DATES
            current = best.get(hit.record.uri)
            if current is None or score > current.score:
                best[hit.record.uri] = Candidate(
                    hit.record,
                    score,
                    False,
                    _SIMILAR_REASONS[hit.via],
                )
        ranked = sorted(best.values(), key=lambda c: c.score, reverse=True)
        return ranked[: query.limit]


_SIMILAR_REASONS = {
    "authorized": "has a heading like this",
    "earlier": "has an earlier form like this",
    "variant": "has a variant like this",
}


def similarity(a: str, b: str) -> float:
    """How alike two label keys are, from 0 to 1.

    The mean of difflib's character ratio, which forgives small differences
    in spelling, and the share of words the two have in common, which keeps a
    longer label that merely starts the same way ("Science fiction,
    Scottish") from outscoring the one asked for ("Science fiction").
    """
    chars = SequenceMatcher(None, a, b).ratio()
    words_a, words_b = set(_WORD.findall(a)), set(_WORD.findall(b))
    union = words_a | words_b
    words = len(words_a & words_b) / len(union) if union else 0.0
    return (chars + words) / 2


def _without_numbers(key: str) -> str:
    return _NUMBERS.sub("", key).strip() or key


def _years(record: AuthorityRecord) -> set[str]:
    """The years in a record's heading, and of its RWO's birth and death."""
    years = set(_YEAR.findall(record.label or ""))
    years |= {date[:4] for date in (record.birth, record.death) if date}
    return years


def _default_schemes(types: frozenset[str] | None) -> frozenset[str]:
    implied = {_SCHEME_OF_KIND[k] for k in types or () if k in _SCHEME_OF_KIND}
    if types and len(implied) == len(types):  # nothing a general scheme has
        return frozenset(implied)
    return GENERAL_SCHEMES | implied


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
