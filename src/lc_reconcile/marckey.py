"""Compare headings as MARC fields, subfield by subfield.

A marcKey is a MARC heading field written out as one string: the tag, two
indicators, then the subfields, e.g. ``1001 $aVerne, Jules,$d1828-1905``.
id.loc.gov gives one for each authority heading and its variants, and
marc-bibframe gives one for each heading it converts from a bibliographic
record, such as ``7001 $aHetzel, P.-J.$q(Pierre-Jules),$d1814-1886,$eeditor.``

Comparing these is stronger than comparing labels: the subfield boundaries
survive, and the parts of a bibliographic field that are not the heading
(relator terms, linking subfields) can be dropped by code instead of guessed
at in the text.
"""

from __future__ import annotations

from lc_reconcile.normalize import normalize_part

#: The last two digits of the heading tags. Authority 1XX/4XX and
#: bibliographic 1XX/6XX/7XX/8XX tags share them: a 700, a 600 and an
#: authority's 100 are all personal names.
_HEADINGS = {
    "00",  # personal or family name
    "10",  # corporate name
    "11",  # meeting name
    "30",  # uniform title
    "47",  # named event
    "48",  # chronological term
    "50",  # topical term
    "51",  # geographic name
    "55",  # genre/form term
    "80",  # general subdivision
    "81",  # geographic subdivision
    "82",  # chronological subdivision
    "85",  # form subdivision
}

#: The MADS kinds of heading each tag can stand for. A name with a title
#: ($t) is a NameTitle; a named event (X47) has no MADS kind of its own.
_KINDS = {
    "00": frozenset({"PersonalName", "FamilyName", "NameTitle"}),
    "10": frozenset({"CorporateName", "NameTitle"}),
    "11": frozenset({"ConferenceName", "NameTitle"}),
    "30": frozenset({"Title"}),
    "48": frozenset({"Temporal"}),
    "50": frozenset({"Topic"}),
    "51": frozenset({"Geographic", "HierarchicalGeographic"}),
    "55": frozenset({"GenreForm"}),
}

#: Subfields that are about the field rather than part of the heading:
#: linking and source subfields ($0-$8), and $w and $i, which say how a
#: variant relates to the heading.
_NOT_HEADING = set("012345678wi")

#: Relator subfields, which say what part a name played in a work and so
#: appear only in bibliographic fields. A meeting name's $e is its
#: subordinate unit; its relator is $j.
_RELATORS = {"00": set("eu"), "10": set("eu"), "11": set("ju"), "30": set()}

#: Subfields that subdivide a heading, making it a ComplexSubject in MADS.
SUBDIVISIONS = set("vxyz")


def parse(marc_key: str) -> tuple[str, str, list[tuple[str, str]]] | None:
    """Split a marcKey into its tag, indicators and ``(code, value)`` subfields."""
    tag, indicators = marc_key[:3], marc_key[3:5]
    start = marc_key.find("$")
    if not tag.isdigit() or start < 0:
        return None
    subfields = [
        (chunk[0], chunk[1:]) for chunk in marc_key[start + 1 :].split("$") if chunk
    ]
    return tag, indicators, subfields


def heading_key(marc_key: str) -> str | None:
    """The comparison key for a heading given as a marcKey.

    The tag becomes its authority 1XX, indicators are dropped, and each
    subfield of the heading itself is normalized on its own and kept with its
    code: ``7001 $aVerne, Jules,$d1828-1905,$eauthor.`` gives
    ``100$averne, jules$d1828 1905``. Keys always contain ``$``, which a
    normalized label never does, so both can share one lookup.
    """
    parsed = parse(marc_key)
    if parsed is None:
        return None
    tag, _, subfields = parsed
    if tag[0] not in "14678" or tag[1:] not in _HEADINGS:
        return None
    dropped = _NOT_HEADING | _RELATORS.get(tag[1:], set())
    parts = []
    for code, value in subfields:
        if code in dropped:
            continue
        value = normalize_part(value)
        if value:
            parts.append(f"${code}{value}")
    if not parts:
        return None
    return "1" + tag[1:] + "".join(parts)


def is_subdivided(key: str) -> bool:
    """Whether a heading key has subdivisions."""
    return any(f"${code}" in key for code in SUBDIVISIONS)


def label_from_marc_key(marc_key: str) -> str:
    """The heading of a marcKey written as a label, as id.loc.gov writes them.

    Subdivisions follow ``--``, and the subfields of the heading itself are
    joined with spaces: ``651 0$aFrance$xHistory$y1789-1815.`` gives
    ``France--History--1789-1815``.
    """
    parsed = parse(marc_key)
    if parsed is None:
        return ""
    tag, _, subfields = parsed
    dropped = _NOT_HEADING | _RELATORS.get(tag[1:], set())
    label = ""
    for code, value in subfields:
        value = value.strip()
        if code in dropped or not value:
            continue
        if not label:
            label = value
        elif code in SUBDIVISIONS:
            label += "--" + value.rstrip(".")
        else:
            label += " " + value
    return label.rstrip(".")


def kinds(marc_key: str) -> frozenset[str] | None:
    """The MADS kinds of heading a marcKey's tag allows, if it says."""
    return _KINDS.get(marc_key[1:3]) if marc_key[:3].isdigit() else None
