"""Reconcile headings against a local, self-updating index of id.loc.gov.

The index is built from LC's bulk snapshots of LCNAF, LCSH and LCGFT
(https://id.loc.gov/download/), so matching a heading never touches
id.loc.gov. Queries and results follow the Reconciliation Service API 0.2,
the protocol OpenRefine speaks.

    >>> from lc_reconcile import Index, Reconciler
    >>> reconciler = Reconciler(Index("lc-reconcile.sqlite"))
    >>> reconciler.match({"q0": {"query": "Science fiction", "type": "Topic"}})
"""

from lc_reconcile.build import SCHEMES, Source, build, default_sources
from lc_reconcile.match import TYPES, Candidate, Query, Reconciler
from lc_reconcile.normalize import normalize
from lc_reconcile.records import AuthorityRecord, iter_records, parse_record
from lc_reconcile.store import Index, IndexUnavailable

__all__ = [
    "SCHEMES",
    "TYPES",
    "AuthorityRecord",
    "Candidate",
    "Index",
    "IndexUnavailable",
    "Query",
    "Reconciler",
    "Source",
    "build",
    "default_sources",
    "iter_records",
    "normalize",
    "parse_record",
]
