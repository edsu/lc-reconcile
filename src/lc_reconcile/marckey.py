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
    "62",  # medium of performance term
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
    "62": frozenset({"Medium"}),
}

#: Source codes ($2) of LC vocabularies other than LCSH and LCNAF, and the
#: scheme each is. https://www.loc.gov/standards/sourcelist/
_SOURCES = {
    "lcgft": "genreForms",
    "lcshac": "childrensSubjects",
    "lcdgt": "demographicTerms",
    "lcmpt": "performanceMediums",
}

#: Source codes of everything the index holds: the vocabularies above, LCSH
#: and LCNAF.
_LC_SOURCES = {*_SOURCES, "lcsh", "naf", "lcnaf"}

#: The second indicator of a bibliographic 6XX that says its heading is from
#: LC's children's subject headings.
_CHILDRENS = "1"

#: Bibliographic subject fields whose second indicator names their
#: thesaurus: 0 LCSH or LCNAF, 1 children's headings, 2 MeSH, 3 NAL,
#: 4 not specified, 5 Canadian, 6 RVM, 7 the one in $2.
_THESAURUS_TAGS = {"600", "610", "611", "630", "647", "648", "650", "651", "655"}
_LC_THESAURI = {"0", _CHILDRENS}

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


def kind(marc_key: str) -> str | None:
    """The one MADS kind an authority marcKey stands for, if it says.

    ``150  $aAmericans`` is a Topic, and with a subdivision a ComplexSubject;
    ``100`` could be a person, a family or a name/title, so says nothing.
    """
    key = heading_key(marc_key)
    if key is not None and is_subdivided(key):
        return "ComplexSubject"
    allowed = kinds(marc_key)
    return next(iter(allowed)) if allowed and len(allowed) == 1 else None


def schemes(marc_key: str) -> frozenset[str] | None:
    """The LC scheme a bibliographic field says its heading is from, if any.

    A ``$2`` source code says so (``655 7$aHorror fiction.$2lcgft``), as does
    the second indicator of a 6XX (``650 1$aCats.`` is a children's
    heading). None means the field doesn't say, or says LCSH or LCNAF.
    """
    parsed = parse(marc_key)
    if parsed is None:
        return None
    tag, indicators, subfields = parsed
    for code in _source_codes(subfields):
        scheme = _SOURCES.get(code)
        if scheme:
            return frozenset({scheme})
    if tag[0] == "6" and indicators[1:2] == _CHILDRENS:
        return frozenset({"childrensSubjects"})
    return None


def from_lc(marc_key: str) -> bool:
    """Whether a field's heading is from a vocabulary the index holds.

    A field is taken at its word: ``650 2`` (MeSH), ``650 4`` (source not
    specified) and ``650 7 ...$2fast`` are not, even when LC has a heading
    with the same words, and a ``653`` is an uncontrolled term. A field
    that doesn't say, such as a ``700``, is taken to be LCNAF.
    """
    parsed = parse(marc_key)
    if parsed is None:
        return True
    tag, indicators, subfields = parsed
    codes = _source_codes(subfields)
    if codes:
        return all(code in _LC_SOURCES for code in codes)
    if tag == "653":
        return False
    if tag in _THESAURUS_TAGS:
        return indicators[1:2] in _LC_THESAURI
    return True


def _source_codes(subfields: list[tuple[str, str]]) -> list[str]:
    return [
        value.strip().rstrip(".").lower() for code, value in subfields if code == "2"
    ]
