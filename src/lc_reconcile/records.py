"""Read MADS/RDF authority records from id.loc.gov N-Triples.

Two sources share this reader: the bulk snapshots at id.loc.gov/download,
where each record is a block between ``# BEGIN /authorities/...`` and
``# END ...`` comments, and the single-record ``{uri}.nt`` documents that a
sync fetches. Neither is parsed into an RDF graph. The names snapshot is 7 GB
compressed, so the reader keeps only the handful of predicates matching needs
and ignores every other line without parsing it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from lc_reconcile import marckey

MADS = "http://www.loc.gov/mads/rdf/v1#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
MARC_KEY = "http://id.loc.gov/ontologies/bflc/marcKey"
LCCN = "http://id.loc.gov/vocabulary/identifiers/lccn"
ID_LOC = "http://id.loc.gov"
RWO_PREFIX = "http://id.loc.gov/rwo/"

#: Predicates the reader keeps. Every other line is skipped unparsed.
_WANTED = {
    RDF_TYPE,
    MARC_KEY,
    LCCN,
    *(
        MADS + local
        for local in (
            "authoritativeLabel",
            "variantLabel",
            "hasVariant",
            "isMemberOfMADSCollection",
            "useInstead",
            "deletionNote",
            "identifiesRWO",
            "birthDate",
            "deathDate",
        )
    ),
}

_DATES = (MADS + "birthDate", MADS + "deathDate")

#: MADS classes that say what kind of heading a record is, most specific
#: first. A record's ``kind`` is the first of these among its types; the rest
#: of its types (Authority, SimpleType, Variant, ...) say nothing about that.
KINDS = (
    "NameTitle",
    "Title",
    "PersonalName",
    "CorporateName",
    "ConferenceName",
    "FamilyName",
    "HierarchicalGeographic",
    "Geographic",
    "ComplexSubject",
    "GenreForm",
    "Medium",
    "Temporal",
    "Topic",
)

#: Resources in a snapshot that describe a scheme or collection rather than
#: a heading, such as LCDGT's "Age" grouping.
_NOT_HEADINGS = {"MADSScheme", "MADSCollection"}

_BEGIN = "# BEGIN "
_END = "# END "

_LITERAL = re.compile(r'"((?:[^"\\]|\\.)*)"(?:@([A-Za-z0-9-]+)|\^\^<[^>]*>)?')
_ESCAPE = re.compile(r"\\(?:u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|(.))")
_SIMPLE_ESCAPES = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f"}


@dataclass(slots=True)
class Label:
    """A literal with its language tag, if any."""

    value: str
    lang: str | None = None


@dataclass(slots=True)
class AuthorityRecord:
    """What matching needs to know about one id.loc.gov authority."""

    uri: str
    scheme: str = ""
    lccn: str | None = None
    kind: str | None = None
    types: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    deprecated: bool = False
    #: The heading itself: the authoritativeLabel with no script subtag.
    label: str | None = None
    #: The heading's own marcKey, the 1XX field as it stands in the record.
    marc_key: str | None = None
    #: Variant headings (4XX), including non-Latin script forms of the heading.
    variants: list[str] = field(default_factory=list)
    #: The variants' marcKeys, where LC gives them.
    variant_keys: list[str] = field(default_factory=list)
    use_instead: list[str] = field(default_factory=list)
    deletion_note: str | None = None
    rwo: str | None = None
    birth: str | None = None
    death: str | None = None


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        code = match.group(1) or match.group(2)
        if code:
            return chr(int(code, 16))
        char = match.group(3)
        return _SIMPLE_ESCAPES.get(char, char)

    return _ESCAPE.sub(replace, text)


def _term(text: str) -> str | Label:
    """An IRI or blank node label as a str, a literal as a Label."""
    if text.startswith("<"):
        return _unescape(text[1 : text.rindex(">")])
    if text.startswith("_:"):
        return text.split()[0]
    match = _LITERAL.match(text)
    if match is None:
        raise ValueError(f"not an N-Triples object: {text!r}")
    return Label(_unescape(match.group(1)), match.group(2))


def _subject(text: str) -> str:
    return text[1:-1] if text.startswith("<") else text


def _is_primary_script(label: Label) -> bool:
    """Whether a label is in the heading's own script, not a script variant.

    id.loc.gov gives a name's non-Latin forms as further authoritativeLabels
    tagged ``@und-cyrl``, ``@ja-jpan`` and so on; they are 4XX fields in the
    MARC. The heading itself carries no tag, or (in LCSH) ``@en``.
    """
    return label.lang is None or "-" not in label.lang


def parse_record(uri: str, lines: Iterable[str]) -> AuthorityRecord | None:
    """Build a record from the N-Triples about ``uri``.

    ``lines`` may hold triples about other resources too -- a single-record
    document carries labels for the headings it links to -- and those are
    ignored. Returns None if nothing in ``lines`` describes ``uri``.
    """
    record = AuthorityRecord(uri=uri)
    found = False
    variant_nodes: list[str] = []
    variant_labels: dict[str, list[str]] = {}
    variant_node_keys: dict[str, list[str]] = {}
    labels: list[Label] = []
    keys: list[str] = []
    # Dates belong to the real-world object, a different subject in the same
    # document, so they are held until the record says which subject that is.
    dates: dict[tuple[str, str], str] = {}

    for line in lines:
        if not line or line[0] == "#":
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        predicate = parts[1][1:-1]
        if predicate not in _WANTED:
            continue
        subject = _subject(parts[0])
        obj = _term(parts[2])

        if subject.startswith("_:"):
            if isinstance(obj, Label):
                if predicate == MADS + "variantLabel":
                    variant_labels.setdefault(subject, []).append(obj.value)
                elif predicate == MARC_KEY:
                    variant_node_keys.setdefault(subject, []).append(obj.value)
            continue
        if subject != uri:
            if predicate in _DATES and isinstance(obj, Label):
                dates.setdefault((subject, predicate), obj.value)
            continue

        found = True
        if isinstance(obj, Label):
            if predicate == MARC_KEY:
                keys.append(obj.value)
            elif predicate == LCCN:
                record.lccn = obj.value
            elif predicate == MADS + "authoritativeLabel":
                labels.append(obj)
            elif predicate == MADS + "variantLabel":
                # A deprecated record keeps its old heading as a variantLabel
                # of the record itself.
                record.variants.append(obj.value)
            elif predicate == MADS + "deletionNote":
                record.deletion_note = obj.value
        elif predicate == RDF_TYPE:
            if obj.startswith(MADS):
                record.types.append(obj[len(MADS) :])
        elif predicate == MADS + "hasVariant":
            variant_nodes.append(obj)
        elif predicate == MADS + "isMemberOfMADSCollection":
            record.collections.append(obj.rsplit("/", 1)[-1])
        elif predicate == MADS + "useInstead":
            record.use_instead.append(obj)
        elif predicate == MADS + "identifiesRWO" and obj.startswith(RWO_PREFIX):
            # Names also identify their RWO by ISNI and others; we want LC's.
            record.rwo = obj

    if not found or _NOT_HEADINGS & set(record.types):
        return None

    record.deprecated = "DeprecatedAuthority" in record.types
    record.kind = next((k for k in KINDS if k in record.types), None)
    # Taken from the URI rather than isMemberOfMADSScheme, which deprecated
    # records lack.
    record.scheme = _scheme_from_uri(uri)

    primary = [label for label in labels if _is_primary_script(label)]
    if primary:
        record.label = primary[0].value
    for label in labels:
        if label.value != record.label:
            record.variants.append(label.value)
    for node in variant_nodes:
        record.variants.extend(variant_labels.get(node, ()))

    # The heading's own marcKey is the 1XX; the 4XXs are its script forms.
    record.marc_key = next((k for k in keys if k.startswith("1")), None)
    record.variant_keys = [k for k in keys if k.startswith("4")]
    if record.kind is None and record.marc_key:
        # LCDGT's records are typed only as Authority; the tag says the rest.
        record.kind = marckey.kind(record.marc_key)
    for node in variant_nodes:
        record.variant_keys.extend(variant_node_keys.get(node, ()))

    if record.rwo:
        record.birth = dates.get((record.rwo, MADS + "birthDate"))
        record.death = dates.get((record.rwo, MADS + "deathDate"))

    # Variants can repeat the heading or each other; keep the first of each.
    seen = {record.label}
    variants = []
    for variant in record.variants:
        if variant not in seen:
            seen.add(variant)
            variants.append(variant)
    record.variants = variants
    return record


def _scheme_from_uri(uri: str) -> str:
    # http://id.loc.gov/authorities/{scheme}/{id}
    parts = uri.split("/")
    return parts[4] if len(parts) > 5 else ""


def iter_blocks(lines: Iterable[str]) -> Iterator[tuple[str, list[str]]]:
    """Split a bulk snapshot into ``(uri, lines)``, one per record."""
    uri: str | None = None
    block: list[str] = []
    for line in lines:
        if line.startswith(_BEGIN):
            uri = ID_LOC + line[len(_BEGIN) :].strip()
            block = []
        elif line.startswith(_END):
            if uri is not None:
                yield uri, block
            uri = None
            block = []
        elif uri is not None:
            block.append(line)


def iter_records(lines: Iterable[str]) -> Iterator[AuthorityRecord]:
    """Every authority record in a bulk snapshot."""
    for uri, block in iter_blocks(lines):
        record = parse_record(uri, block)
        if record is not None:
            yield record
