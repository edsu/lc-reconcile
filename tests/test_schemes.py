"""LC's smaller vocabularies: children's subjects, LCDGT and LCMPT.

They repeat many headings of LCSH and LCNAF, so a query reaches them only
when it asks for one.
"""

import pytest

from lc_reconcile import Query, Reconciler, iter_records, marckey
from lc_reconcile.match import TYPES

from .conftest import FIXTURES

ID = "http://id.loc.gov/authorities/"
LCSH_SCIENCE_FICTION = ID + "subjects/sh85118629"
CHILDRENS_SCIENCE_FICTION = ID + "childrensSubjects/sj2021050042"
CHILDRENS_CATS = ID + "childrensSubjects/sj96004989"
AMERICANS = ID + "demographicTerms/dg2015060001"
PIANO = ID + "performanceMediums/mp2013015550"


def _records(scheme):
    lines = (FIXTURES / f"{scheme}.nt").read_text(encoding="utf-8").splitlines()
    return {record.uri: record for record in iter_records(lines)}


def test_demographic_terms_are_topics():
    records = _records("demographicTerms")
    # The "Age" collection is not a heading, and is left out.
    assert list(records) == [AMERICANS]
    americans = records[AMERICANS]
    assert (americans.label, americans.kind) == ("Americans", "Topic")


def test_mediums_of_performance():
    piano = _records("performanceMediums")[PIANO]
    assert (piano.label, piano.kind, piano.marc_key) == (
        "piano",
        "Medium",
        "162  $apiano",
    )
    assert marckey.heading_key(piano.marc_key) == "162$apiano"


@pytest.mark.parametrize(
    ("field", "schemes"),
    [
        ("650 1$aCats.", {"childrensSubjects"}),
        ("650 0$aCats.", None),
        ("650 7$aCats.$2lcshac", {"childrensSubjects"}),
        ("655 7$aHorror fiction.$2lcgft", {"genreForms"}),
        ("385  $aAmericans$2lcdgt", {"demographicTerms"}),
        ("382 01$apiano$n1$2lcmpt", {"performanceMediums"}),
        ("650 7$aCats.$2fast", None),
        ("7001 $aVerne, Jules,$d1828-1905.", None),
    ],
)
def test_schemes_a_field_names(field, schemes):
    found = marckey.schemes(field)
    assert (set(found) if found else None) == schemes


@pytest.fixture
def reconciler(index) -> Reconciler:
    return Reconciler(index)


def _ask(reconciler, text, type_=None, **kwargs):
    query = Query(text, types=TYPES[type_] if type_ else None, **kwargs)
    return [(c.record.uri, c.match) for c in reconciler.candidates(query)]


def test_general_schemes_by_default(reconciler):
    # Children's subjects has its own "Science fiction" and "Cats".
    assert _ask(reconciler, "Science fiction") == [(LCSH_SCIENCE_FICTION, True)]
    assert _ask(reconciler, "Cats", "Topic") == []


def test_asking_for_a_scheme(reconciler):
    childrens = frozenset({"childrensSubjects"})
    assert _ask(reconciler, "Science fiction", schemes=childrens) == [
        (CHILDRENS_SCIENCE_FICTION, True)
    ]
    assert _ask(reconciler, "Kittens", schemes=childrens) == [(CHILDRENS_CATS, False)]
    both = frozenset({"subjects", "childrensSubjects"})
    assert {uri for uri, _ in _ask(reconciler, "Science fiction", schemes=both)} == {
        LCSH_SCIENCE_FICTION,
        CHILDRENS_SCIENCE_FICTION,
    }


def test_marc_key_says_which_scheme(reconciler):
    assert _ask(reconciler, "", marc_key="650 1$aScience fiction.") == [
        (CHILDRENS_SCIENCE_FICTION, True)
    ]
    assert _ask(reconciler, "", marc_key="650 0$aScience fiction.") == [
        (LCSH_SCIENCE_FICTION, True)
    ]
    # A 385 isn't a heading field, so it's matched by its label.
    assert _ask(reconciler, "", marc_key="385  $aAmericans$2lcdgt") == [
        (AMERICANS, True)
    ]


def test_medium_type_finds_its_scheme(reconciler):
    assert _ask(reconciler, "Piano", "Medium") == [(PIANO, True)]
    assert _ask(reconciler, "Piano") == []
    assert _ask(reconciler, "", marc_key="162  $apiano") == [(PIANO, True)]
