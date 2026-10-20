import pytest

from lc_reconcile.marckey import (
    from_lc,
    heading_key,
    is_subdivided,
    kinds,
    label_from_marc_key,
    parse,
)


def test_parse():
    assert parse("1001 $aVerne, Jules,$d1828-1905") == (
        "100",
        "1 ",
        [("a", "Verne, Jules,"), ("d", "1828-1905")],
    )


@pytest.mark.parametrize(
    ("bibliographic", "authority"),
    [
        # Relator terms and codes are dropped, and 7XX maps to 1XX.
        (
            "7001 $aHetzel, P.-J.$q(Pierre-Jules),$d1814-1886,$eeditor.$4edt",
            "1001 $aHetzel, P.-J.$q(Pierre-Jules),$d1814-1886",
        ),
        # Subject fields, whatever their indicators, and with their source.
        ("650 0$aScience fiction.", "150  $aScience fiction"),
        ("651 7$aFrance$xHistory.$2fast", "151 0$aFrance$xHistory"),
        ("655 7$aFiction.$2lcgft", "155  $aFiction"),
        # A variant's $w and a script form's $7 are not part of the heading.
        ("4001 $wnne$aKalmykova, V. G.", "1001 $aKalmykova, V. G."),
        (
            "4001 $aVern, Jul,$d1828-1905$7(bcp47)und-latn",
            "1001 $aVern, Jul,$d1828-1905",
        ),
        # Linking subfields.
        (
            "6001 $aVerne, Jules,$d1828-1905$0http://id.loc.gov/x",
            "1001 $aVerne, Jules,$d1828-1905",
        ),
    ],
)
def test_same_heading(bibliographic, authority):
    assert heading_key(bibliographic) == heading_key(authority)


def test_key():
    assert (
        heading_key("1001 $aVerne, Jules,$d1828-1905") == "100$averne, jules$d1828 1905"
    )


def test_meeting_name_subordinate_unit_is_kept():
    # $e of a meeting name is a subordinate unit; its relator is $j.
    assert heading_key("7112 $aOlympic Games$eOrganizing Committee,$jhost.") == (
        "111$aolympic games$eorganizing committee"
    )


def test_subfield_boundaries_matter():
    assert heading_key("150  $aFrance$xHistory") != heading_key("150  $aFrance history")


def test_tags_that_are_not_headings():
    assert heading_key("245 10$aTwenty thousand leagues under the sea") is None
    assert heading_key("500  $aSee also Verne") is None
    assert heading_key("not a marcKey") is None
    assert heading_key("1001 $eauthor") is None


def test_subdivided():
    assert is_subdivided("151$afrance$xhistory")
    assert not is_subdivided("150$ascience fiction")


def test_label_from_marc_key():
    assert label_from_marc_key("651 0$aFrance$xHistory$y1789-1815.") == (
        "France--History--1789-1815"
    )
    assert label_from_marc_key("7001 $aVerne, Jules,$d1828-1905,$eauthor.") == (
        "Verne, Jules, 1828-1905,"
    )


def test_kinds():
    assert kinds("7102 $aGreat Britain.$bArmy") == {"CorporateName", "NameTitle"}
    assert kinds("655 7$aFiction.$2lcgft") == {"GenreForm"}
    assert kinds("6470 $aBattle of Hastings") is None


@pytest.mark.parametrize(
    ("field", "lc"),
    [
        ("650 0$aCats.", True),
        ("650 1$aCats.", True),
        ("650 2$aCats.", False),
        ("650 4$aCats.", False),
        ("650 6$aChats.", False),
        ("650 7$aCats.$2fast", False),
        ("650 7$aCats.$2lcsh", True),
        ("655 7$aHorror fiction.$2lcgft", True),
        ("650 7$aCats.", False),
        ("653  $aCats", False),
        ("385  $aAmericans$2lcdgt", True),
        ("7001 $aVerne, Jules,$d1828-1905.", True),
    ],
)
def test_from_lc(field, lc):
    assert from_lc(field) is lc
