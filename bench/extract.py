"""Sample headings from LC BIBFRAME records, with the authorities LC linked.

    uv run python bench/extract.py ../bluecore-models/uploads/batch_0000*.tar.gz

reads the ``*.rdf`` records in the tarballs (LC's BIBFRAME descriptions of
bibliographic records) and writes ``bench/headings.jsonl``: one line per
agent, subject or genre/form heading, with the marcKey and label LC gives it
and, where LC linked it to an authority, that authority. ``link`` says what
LC linked it to: ``lc`` (one of the authorities), ``fast``, ``other`` (a hub,
another vocabulary) or null (nothing). The links are what LC's
own cataloguing decided, so they are the answers a benchmark can check
against. Headings LC built without an authority (a subject with free
subdivisions, say) are kept too, with no answer: they are the queries that
fall through to the similar-heading search.

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
from pathlib import Path

RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
RDFS = "{http://www.w3.org/2000/01/rdf-schema#}"
BF = "{http://id.loc.gov/ontologies/bibframe/}"
BFLC = "{http://id.loc.gov/ontologies/bflc/}"

AUTHORITIES = "http://id.loc.gov/authorities/"
AGENTS = "http://id.loc.gov/rwo/agents/"

#: The properties whose objects are headings, and what each heading is.
ROLES = {
    BF + "agent": "agent",
    BF + "subject": "subject",
    BF + "genreForm": "genreForm",
}

OUT = Path(__file__).with_name("headings.jsonl")


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


def headings(record: bytes) -> list[dict[str, str | None]]:
    found: list[dict[str, str | None]] = []
    root = ET.fromstring(record)
    for prop, role in ROLES.items():
        for holder in root.iter(prop):
            for node in holder:
                marc_key = node.findtext(BFLC + "marcKey")
                if not marc_key or marc_key[:1] not in "167":
                    continue
                about = node.get(RDF + "about")
                expected = authority(about)
                found.append(
                    {
                        "role": role,
                        "marc_key": marc_key,
                        "label": (node.findtext(RDFS + "label") or "").strip(),
                        "expected": expected,
                        "link": _link(about, expected),
                    }
                )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tarballs", nargs="+", type=Path)
    parser.add_argument(
        "--share", type=float, default=0.012, help="share of each tarball's records"
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    rng = random.Random(args.seed)
    rows: list[dict[str, str | None]] = []
    for path in args.tarballs:
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
                except ET.ParseError as error:
                    print(f"skipping {member.name}: {error}", file=sys.stderr)
    with args.out.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    roles: dict[str, int] = {}
    for row in rows:
        roles[str(row["role"])] = roles.get(str(row["role"]), 0) + 1
    links: dict[str, int] = {}
    for row in rows:
        links[str(row["link"])] = links.get(str(row["link"]), 0) + 1
    print(
        f"wrote {len(rows):,} headings to {args.out}: "
        + ", ".join(f"{n:,} {role}" for role, n in sorted(roles.items()))
        + "; linked to "
        + ", ".join(f"{link} {n:,}" for link, n in sorted(links.items())),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
