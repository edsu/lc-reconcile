"""Sample headings from LC BIBFRAME records, with the authorities LC linked.

    uv run python bench/extract.py ../bluecore-models/uploads/batch_*.tar.gz

reads the ``*.rdf`` records in the tarballs (LC's BIBFRAME descriptions of
bibliographic records) and writes ``bench/headings.jsonl``: one line per
contributor, subject or genre/form heading, with its label, its classes
(``Person``, ``Topic`` and ``ComplexSubject``, ...), the marcKey LC gives it
if it has one, and, where LC linked it to an authority, that authority.

A heading is what a description gives: a label and a class. A marcKey is
only left over from converting MARC, so a description made as BIBFRAME has
none, and a benchmark queries by label and type first.

``link`` says what LC linked the heading to: ``lc`` (one of the
authorities), ``fast``, ``other`` (a hub, another vocabulary) or null
(nothing). The links are what LC's own cataloguing decided, so they are the
answers a benchmark can check against. Headings LC didn't link are kept
too, with no answer -- a subject with subdivisions LC never established as
one heading, say: they are the queries that fall through to the
similar-heading search. Headings that say they are from a vocabulary other
than LC's (RVM, MeSH, BISAC, ...) are left out, and so are uncontrolled
ones, and those with no label.

The sample is a fixed share of each tarball's records, so it is the same
each time and stays small enough to keep in git.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tarfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RDFS = "{http://www.w3.org/2000/01/rdf-schema#}"
BF = "{http://id.loc.gov/ontologies/bibframe/}"
BFLC = "{http://id.loc.gov/ontologies/bflc/}"
MADS = "{http://www.loc.gov/mads/rdf/v1#}"

AUTHORITIES = "http://id.loc.gov/authorities/"
AGENTS = "http://id.loc.gov/rwo/agents/"

#: Codes a heading's source can give for one of LC's vocabularies.
LC_CODES = {"lcsh", "lcgft", "naf", "lcnaf", "lcshac", "lcdgt", "lcmpt"}

OUT = Path(__file__).with_name("headings.jsonl")

Row = dict[str, object]


def authority(uri: str | None) -> str | None:
    """The authority a linked resource stands for, if it is one we index.

    A name is linked by its real-world object (``rwo/agents/n…``), whose
    authority has the same id. ``n78095330-781`` is a name used as a
    geographic subdivision, which isn't an authority of its own.
    """
    if not uri:
        return None
    uri = uri.replace("https://", "http://", 1)
    if uri.startswith(AGENTS):
        id_ = uri[len(AGENTS) :]
        return None if "-" in id_ else AUTHORITIES + "names/" + id_
    if uri.startswith(AUTHORITIES):
        return uri
    return None


def _link(about: str | None, expected: str | None) -> str | None:
    if expected:
        return "lc"
    if about is None:
        return None
    return "fast" if "/fast/" in about else "other"


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def classes(node: ET.Element) -> list[str]:
    """A heading's classes: its element's, and its ``rdf:type``s."""
    found = {_local(node.tag)}
    for type_ in node.findall(RDF + "type"):
        if resource := type_.get(RDF + "resource"):
            found.add(_local(resource))
    return sorted(found)


def schemes(node: ET.Element) -> list[str]:
    """The vocabularies a heading says it is from, as URIs or codes."""
    found = []
    for holder in node.findall(MADS + "isMemberOfMADSScheme") + node.findall(
        BF + "source"
    ):
        value = holder.get(RDF + "resource")
        for child in holder:
            value = value or child.get(RDF + "about") or child.findtext(BF + "code")
        if value:
            found.append(value.strip())
    return found


def from_lc(schemes: list[str]) -> bool:
    """Whether a heading's vocabularies, if it gives any, are LC's."""
    return all(
        scheme.replace("https://", "http://", 1).startswith(AUTHORITIES)
        or scheme.lower() in LC_CODES
        for scheme in schemes
    )


def _nodes(root: ET.Element) -> list[tuple[str, ET.Element]]:
    """The heading nodes in a record, with what each is a heading of.

    Agents are taken from contributions only: the cataloguing agencies in
    ``adminMetadata`` are agents too.
    """
    found = []
    for contribution in root.iter(BF + "contribution"):
        for holder in contribution.iter(BF + "agent"):
            found += [("agent", node) for node in holder]
    for prop, role in ((BF + "subject", "subject"), (BF + "genreForm", "genreForm")):
        for holder in root.iter(prop):
            found += [(role, node) for node in holder]
    return found


def headings(record: bytes) -> list[Row]:
    found: list[Row] = []
    for role, node in _nodes(ET.fromstring(record)):
        label = " ".join((node.findtext(RDFS + "label") or "").split())
        types = classes(node)
        claimed = schemes(node)
        if not label or "Uncontrolled" in types or not from_lc(claimed):
            continue
        about = node.get(RDF + "about")
        expected = authority(about)
        found.append(
            {
                "role": role,
                "label": label,
                "types": types,
                "marc_key": node.findtext(BFLC + "marcKey"),
                "expected": expected,
                "link": _link(about, expected),
            }
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tarballs", nargs="+", type=Path)
    parser.add_argument(
        "--share",
        type=float,
        default=0.002,
        help="share of each tarball's records (default: 0.002)",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    rows: list[Row] = []
    records = 0
    for path in sorted(args.tarballs):
        with tarfile.open(path) as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith(".rdf"):
                    continue
                if rng.random() >= args.share:
                    continue
                data = tar.extractfile(member)
                if data is None:
                    continue
                try:
                    rows.extend(headings(data.read()))
                    records += 1
                except ET.ParseError as error:
                    print(f"skipping {member.name}: {error}", file=sys.stderr)
    with args.out.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    roles = Counter(str(row["role"]) for row in rows)
    links = Counter(str(row["link"]) for row in rows)
    keyed = sum(1 for row in rows if row["marc_key"])
    print(
        f"wrote {len(rows):,} headings from {records:,} records to {args.out}: "
        + ", ".join(f"{n:,} {role}" for role, n in sorted(roles.items()))
        + "; linked to "
        + ", ".join(f"{link} {n:,}" for link, n in sorted(links.items()))
        + f"; {keyed:,} with a marcKey",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
