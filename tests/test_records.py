"""The records reader, checked against rdflib's reading of the same data."""

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from lc_reconcile import iter_records, parse_record
from lc_reconcile.records import MADS, MARC_KEY, iter_blocks

from .conftest import FIXTURES, VERNE


def _mads(local: str) -> URIRef:
    return URIRef(MADS + local)


def _expected(uri: str, graph: Graph) -> dict:
    """What the reader should find, worked out the slow way."""
    node = URIRef(uri)
    labels = list(graph.objects(node, _mads("authoritativeLabel")))
    primary = [
        str(label)
        for label in labels
        if isinstance(label, Literal)
        and (not label.language or "-" not in label.language)
    ]
    variants = {str(label) for label in labels} - set(primary[:1])
    variants |= {str(v) for v in graph.objects(node, _mads("variantLabel"))}
    for variant in graph.objects(node, _mads("hasVariant")):
        variants |= {str(v) for v in graph.objects(variant, _mads("variantLabel"))}
    types = {
        str(t)[len(MADS) :]
        for t in graph.objects(node, RDF.type)
        if str(t).startswith(MADS)
    }
    keys = [str(k) for k in graph.objects(node, URIRef(MARC_KEY))]
    variant_keys = {k for k in keys if k.startswith("4")}
    for variant in graph.objects(node, _mads("hasVariant")):
        variant_keys |= {str(k) for k in graph.objects(variant, URIRef(MARC_KEY))}
    keys = [k for k in keys if k.startswith("1")]
    earlier = list(graph.objects(node, _mads("hasEarlierEstablishedForm")))
    return {
        "label": primary[0] if primary else None,
        "variants": variants - {primary[0] if primary else None},
        "types": types,
        "collections": {
            str(c).rsplit("/", 1)[-1]
            for c in graph.objects(node, _mads("isMemberOfMADSCollection"))
        },
        "use_instead": {str(u) for u in graph.objects(node, _mads("useInstead"))},
        "marc_key": keys[0] if keys else None,
        "variant_keys": variant_keys,
        "earlier": {
            str(v) for e in earlier for v in graph.objects(e, _mads("variantLabel"))
        },
        "earlier_keys": {
            str(k) for e in earlier for k in graph.objects(e, URIRef(MARC_KEY))
        },
    }


def _actual(record) -> dict:
    return {
        "label": record.label,
        "variants": set(record.variants),
        "types": set(record.types),
        "collections": set(record.collections),
        "use_instead": set(record.use_instead),
        "marc_key": record.marc_key,
        "variant_keys": set(record.variant_keys),
        "earlier": set(record.earlier),
        "earlier_keys": set(record.earlier_keys),
    }


def test_snapshot_blocks_agree_with_rdflib():
    checked = 0
    for scheme in ("names", "subjects", "genreForms"):
        path = FIXTURES / f"{scheme}.nt"
        for uri, lines in iter_blocks(
            path.read_text(encoding="utf-8").splitlines(keepends=True)
        ):
            graph = Graph().parse(data="".join(lines), format="nt")
            record = parse_record(uri, lines)
            assert record is not None
            assert _actual(record) == _expected(uri, graph), uri
            checked += 1
    assert checked == 19


def test_single_record_document_agrees_with_rdflib():
    path = FIXTURES / "n79064013.nt"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    # LC's document types iso639-2/eng as <//www.loc.gov/mads/rdf/v1#Authority>,
    # a relative IRI that N-Triples doesn't allow. rdflib rejects the whole
    # document for it; the reader passes over it, since it isn't about Verne.
    bad = [line for line in lines if " <//" in line]
    assert bad
    graph = Graph().parse(
        data="".join(line for line in lines if line not in bad), format="nt"
    )
    record = parse_record(
        VERNE, path.read_text(encoding="utf-8").splitlines(keepends=True)
    )
    assert record is not None
    assert _actual(record) == _expected(VERNE, graph)


def test_verne():
    record = parse_record(
        VERNE,
        (FIXTURES / "n79064013.nt")
        .read_text(encoding="utf-8")
        .splitlines(keepends=True),
    )
    assert record is not None
    assert record.scheme == "names"
    assert record.kind == "PersonalName"
    assert record.label == "Verne, Jules, 1828-1905"
    assert record.marc_key == "1001 $aVerne, Jules,$d1828-1905"
    # Non-Latin script forms of the heading are variants, not the heading.
    assert "Верн, Жюль, 1828-1905" in record.variants
    assert "Verne, Julio, 1828-1905" in record.variants
    # LC's own RWO, not the ISNI one the record also identifies it by.
    assert record.rwo == "http://id.loc.gov/rwo/agents/n79064013"
    assert record.birth == "1828-02-08"
    assert record.death == "1905-04-24"
    assert "4001 $aVern, Jul,$d1828-1905$7(bcp47)und-latn" in record.variant_keys
    assert "4001 $aВерн, Жюль,$d1828-1905$7(bcp47)und-cyrl" in record.variant_keys


def test_deprecated_record():
    records = {
        r.uri: r
        for r in iter_records(
            (FIXTURES / "genreForms.nt")
            .read_text(encoding="utf-8")
            .splitlines(keepends=True)
        )
    }
    old = records["http://id.loc.gov/authorities/genreForms/gf2011025006"]
    assert old.deprecated
    assert old.label is None
    assert old.variants == ["Gay pornographic films"]
    assert old.use_instead == [
        "http://id.loc.gov/authorities/genreForms/gf2011026460",
        "http://id.loc.gov/authorities/genreForms/gf2018026031",
    ]
    assert old.deletion_note and old.deletion_note.startswith("This authority record")


def test_escapes():
    line = (
        "<http://id.loc.gov/authorities/subjects/sh1> "
        "<http://www.loc.gov/mads/rdf/v1#authoritativeLabel> "
        '"Caf\\u00E9 \\"society\\""@en .\n'
    )
    lines = [line]
    record = parse_record("http://id.loc.gov/authorities/subjects/sh1", lines)
    assert record is not None
    assert record.label == 'Café "society"'


def test_nothing_about_the_uri():
    assert parse_record(VERNE, ["# a comment\n", "\n"]) is None


def test_earlier_established_forms():
    records = {
        r.uri: r
        for r in iter_records(
            (FIXTURES / "genreForms.nt").read_text(encoding="utf-8").splitlines()
        )
    }
    gay_films = records["http://id.loc.gov/authorities/genreForms/gf2018026031"]
    assert set(gay_films.earlier) == {"Gay pornographic films", "Gay erotic films"}
