"""Turn headings into comparison keys.

Two headings that a cataloguer would call the same should get the same key,
and two that differ in substance should not. The rules follow NACO
normalization (https://www.loc.gov/aba/pcc/naco/normrule-2.html) in spirit:
case, diacritics and most punctuation are ignored; letters, digits, and the
first comma of each part (which separates a surname from forenames) are kept.

One difference: NACO normalizes each MARC subfield separately, but a
heading's label has already lost its subfield boundaries, except for the
``--`` that separates subdivisions. So a label is split on ``--`` and each
part normalized on its own.
"""

from __future__ import annotations

import re
import unicodedata

#: Letters that do not decompose under NFKD but that NACO folds to ASCII.
_FOLD = str.maketrans(
    {
        "æ": "ae",
        "Æ": "ae",
        "œ": "oe",
        "Œ": "oe",
        "ø": "o",
        "Ø": "o",
        "đ": "d",
        "Đ": "d",
        "ð": "d",
        "Ð": "d",
        "ł": "l",
        "Ł": "l",
        "þ": "th",
        "Þ": "th",
        "ı": "i",
    }
)

#: Deleted outright, so "O'Brien" and "OBrien" agree. Includes the modifier
#: letters ALA-LC romanization uses for soft and hard signs and for ayn/alif.
_DELETE = re.compile(r"[\[\]'’ʹʺʻʼʾʿ]")

#: Everything else that is neither a word character nor one of the symbols
#: NACO keeps becomes a space.
_SPACE = re.compile(r"[^\w&#+,]|_")
_SPACES = re.compile(r"\s+")

_SUBDIVISION = re.compile(r"\s*--\s*")


def normalize_part(text: str) -> str:
    """Normalize one part of a heading: a subfield, or a span between ``--``."""
    text = unicodedata.normalize("NFKD", text.translate(_FOLD))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _DELETE.sub("", text.casefold())
    text = _SPACE.sub(" ", text)
    # Keep the first comma only; the rest are punctuation like any other.
    head, comma, tail = text.partition(",")
    if comma:
        text = head.rstrip() + "," + tail.replace(",", " ")
    text = _SPACES.sub(" ", text).strip()
    return text.rstrip(",").rstrip()


def normalize(heading: str) -> str:
    """The comparison key for a heading label such as ``Verne, Jules, 1828-1905``.

    Subdivisions keep their ``--`` separator, so ``France--History`` and
    ``France history`` stay apart.
    """
    parts = [normalize_part(part) for part in _SUBDIVISION.split(heading.strip())]
    return "--".join(part for part in parts if part)
