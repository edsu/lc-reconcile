import pytest

from lc_reconcile import normalize


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Verne, Jules, 1828-1905", "verne, jules, 1828-1905."),
        ("Dvořák, Antonín, 1841-1904", "Dvorak, Antonin, 1841-1904"),
        ("O'Brien, Flann, 1911-1966", "OBrien, Flann, 1911-1966"),
        ("Æsop", "aesop"),
        ("Korean Americans -- Fiction", "Korean Americans--Fiction"),
        ("Science fiction", "  Science   fiction "),
        ("Ẓāhirī, ʻAlī", "Zahiri, Ali"),
    ],
)
def test_same_heading_same_key(a, b):
    assert normalize(a) == normalize(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # Dates distinguish people; a heading without them is another heading.
        ("Verne, Jules", "Verne, Jules, 1828-1905"),
        # Subdivisions keep their place.
        ("France--History", "France history"),
        # The first comma separates surname from forenames.
        ("Hall, Peter", "Hall Peter"),
        # NACO keeps these symbols.
        ("C++ (Computer program language)", "C (Computer program language)"),
    ],
)
def test_different_heading_different_key(a, b):
    assert normalize(a) != normalize(b)


def test_key_shape():
    assert normalize("Verne, Jules, 1828-1905") == "verne, jules 1828 1905"
    assert normalize("Angiotensin II--Antagonists") == "angiotensin ii--antagonists"
    assert normalize("--") == ""
