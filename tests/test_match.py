import pytest

from lc_reconcile import Query, Reconciler
from lc_reconcile.match import TYPES

from .conftest import VERNE

SUBJECTS = "http://id.loc.gov/authorities/subjects/"
GENRE_FORMS = "http://id.loc.gov/authorities/genreForms/"


@pytest.fixture
def reconciler(index) -> Reconciler:
    return Reconciler(index)


def _ask(reconciler, text, type_=None, **kwargs):
    query = Query(text, types=TYPES[type_] if type_ else None, **kwargs)
    return reconciler.candidates(query)


def test_authorized_heading_matches(reconciler):
    [candidate] = _ask(reconciler, "Science fiction", "Topic")
    assert candidate.record.uri == SUBJECTS + "sh85118629"
    assert candidate.match
    assert candidate.score == 100


def test_case_and_punctuation_do_not_matter(reconciler):
    [candidate] = _ask(reconciler, "science fiction.", "Topic")
    assert candidate.match


def test_name_with_dates(reconciler):
    [candidate] = _ask(reconciler, "Verne, Jules, 1828-1905", "PersonalName")
    assert candidate.record.uri == VERNE
    assert candidate.match
    assert candidate.to_json()["rwo"] == "http://id.loc.gov/rwo/agents/n79064013"


def test_dates_property_completes_the_heading(reconciler):
    [candidate] = _ask(reconciler, "Verne, Jules", "PersonalName", dates="1828-1905")
    assert candidate.match


def test_name_without_dates_is_not_a_match(reconciler):
    # id.loc.gov's label service redirects this straight to Verne.
    assert _ask(reconciler, "Verne, Jules", "PersonalName") == []


def test_variant_is_a_candidate_not_a_match(reconciler):
    [candidate] = _ask(reconciler, "Verne, Julio, 1828-1905")
    assert candidate.record.uri == VERNE
    assert not candidate.match
    assert candidate.score == 90


def test_unique_variant_can_be_accepted(index):
    reconciler = Reconciler(index, accept_unique_variant=True)
    [candidate] = _ask(reconciler, "Science stories", "Topic")
    assert candidate.match


def test_script_form_is_a_variant(reconciler):
    [candidate] = _ask(reconciler, "Верн, Жюль, 1828-1905")
    assert candidate.record.uri == VERNE
    assert not candidate.match


def test_undifferentiated_name_is_not_a_match(reconciler):
    [candidate] = _ask(reconciler, "Brennan, Michael", "PersonalName")
    assert not candidate.match
    assert "undifferentiated" in candidate.reason


def test_heading_preferred_over_subdivision(reconciler):
    # "Fiction" is a heading and two subdivisions ($v and $x).
    [candidate] = _ask(reconciler, "Fiction", "Topic")
    assert candidate.record.uri == SUBJECTS + "sh85048050"
    assert candidate.match


def test_type_restricts(reconciler):
    [candidate] = _ask(reconciler, "Fiction", "GenreForm")
    assert candidate.record.scheme == "genreForms"
    assert _ask(reconciler, "Science fiction", "PersonalName") == []


def test_scheme_restricts(reconciler):
    [candidate] = _ask(reconciler, "Fiction", schemes=frozenset({"genreForms"}))
    assert candidate.record.uri == GENRE_FORMS + "gf2014026339"


def test_subdivided_heading_is_a_complex_subject(reconciler):
    for type_ in ("Topic", "Geographic", "ComplexSubject"):
        [candidate] = _ask(
            reconciler,
            "Korean Americans -- Fiction"
            if type_ != "Geographic"
            else "France--History",
            type_,
        )
        assert candidate.record.kind == "ComplexSubject"
        assert candidate.match


def test_deprecated_heading_offers_its_replacement(reconciler):
    [candidate] = _ask(reconciler, "Godzilla films", "GenreForm")
    assert candidate.record.uri == GENRE_FORMS + "gf2011026402"  # Monster films
    assert not candidate.match
    assert candidate.score == 70
    assert "gf2011026302" in candidate.reason


def test_variant_wins_over_deprecated(reconciler):
    # LC kept this deprecated heading as a variant of both its replacements,
    # so it is found that way first.
    candidates = _ask(reconciler, "Gay pornographic films", "GenreForm")
    assert [c.record.uri for c in candidates] == [
        GENRE_FORMS + "gf2011026460",
        GENRE_FORMS + "gf2018026031",
    ]
    assert not any(c.match for c in candidates)


def test_deprecated_without_replacement_is_a_miss(reconciler):
    heading = "Environmental justice--Religious aspects--Buddhism, [Christianity, etc.]"
    assert _ask(reconciler, heading) == []


def test_miss(reconciler):
    assert _ask(reconciler, "No such heading anywhere") == []
    assert _ask(reconciler, "") == []


def test_limit(reconciler):
    assert len(_ask(reconciler, "Gay pornographic films", limit=1)) == 1


def test_batch_in_the_protocol_shape(reconciler):
    response = reconciler.match(
        {
            "q0": {"query": "Science fiction", "type": "Topic"},
            "q1": {
                "query": "Verne, Jules",
                "type": "PersonalName",
                "properties": [{"pid": "dates", "v": "1828-1905"}],
            },
            "q2": {
                "query": "Fiction",
                "properties": [{"pid": "scheme", "v": "genreForms"}],
            },
            "q3": {"query": "No such heading"},
        }
    )
    assert response["q0"]["result"][0] == {
        "id": SUBJECTS + "sh85118629",
        "name": "Science fiction",
        "type": [{"id": "Topic", "name": "Topic"}],
        "score": 100,
        "match": True,
        "description": "matches the authorized heading",
    }
    assert response["q1"]["result"][0]["id"] == VERNE
    assert response["q2"]["result"][0]["id"] == GENRE_FORMS + "gf2014026339"
    assert response["q3"] == {"result": []}


def test_unknown_type():
    with pytest.raises(ValueError, match="unknown type"):
        Query.from_json({"query": "x", "type": "Spaceship"})
