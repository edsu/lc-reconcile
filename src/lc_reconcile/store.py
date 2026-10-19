"""The SQLite index of authority records.

One file holds every scheme that has been loaded. Records are stored once in
``authority``; ``label`` maps each normalized heading and variant to the
records that carry it, and is what a lookup reads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from lc_reconcile.normalize import normalize
from lc_reconcile.records import AuthorityRecord

#: Bumped whenever the schema changes; an index of another version is rebuilt.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE authority (
    uri TEXT PRIMARY KEY,
    scheme TEXT NOT NULL,
    kind TEXT,
    label TEXT,
    marc_key TEXT,
    lccn TEXT,
    types TEXT NOT NULL,
    collections TEXT NOT NULL,
    deprecated INTEGER NOT NULL,
    deletion_note TEXT,
    rwo TEXT,
    viaf TEXT,
    birth TEXT,
    death TEXT
) WITHOUT ROWID;

-- kind is 'authorized', 'variant', or 'deprecated' (the old heading of a
-- deprecated record).
CREATE TABLE label (
    key TEXT NOT NULL,
    uri TEXT NOT NULL,
    kind TEXT NOT NULL
);

CREATE TABLE use_instead (
    uri TEXT NOT NULL,
    target TEXT NOT NULL
);

CREATE TABLE scheme (
    name TEXT PRIMARY KEY,
    source TEXT,
    snapshot_modified TEXT,
    built TEXT NOT NULL,
    records INTEGER NOT NULL
);
"""

# Created after a bulk load: maintaining them row by row is far slower.
_INDEXES = """
CREATE INDEX IF NOT EXISTS label_key ON label (key);
CREATE INDEX IF NOT EXISTS label_uri ON label (uri);
CREATE INDEX IF NOT EXISTS use_instead_uri ON use_instead (uri);
"""


def label_rows(record: AuthorityRecord) -> list[tuple[str, str, str]]:
    """The ``label`` rows for a record, one per distinct key."""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(text: str | None, kind: str) -> None:
        if not text:
            return
        key = normalize(text)
        if key and key not in seen:
            seen.add(key)
            rows.append((key, record.uri, kind))

    if record.deprecated:
        for variant in [record.label, *record.variants]:
            add(variant, "deprecated")
    else:
        add(record.label, "authorized")
        for variant in record.variants:
            add(variant, "variant")
    return rows


def _authority_row(record: AuthorityRecord) -> tuple:
    return (
        record.uri,
        record.scheme,
        record.kind,
        record.label,
        record.marc_key,
        record.lccn,
        " ".join(record.types),
        " ".join(record.collections),
        int(record.deprecated),
        record.deletion_note,
        record.rwo,
        record.viaf,
        record.birth,
        record.death,
    )


def _record(row: sqlite3.Row) -> AuthorityRecord:
    return AuthorityRecord(
        uri=row["uri"],
        scheme=row["scheme"],
        kind=row["kind"],
        label=row["label"],
        marc_key=row["marc_key"],
        lccn=row["lccn"],
        types=row["types"].split(),
        collections=row["collections"].split(),
        deprecated=bool(row["deprecated"]),
        deletion_note=row["deletion_note"],
        rwo=row["rwo"],
        viaf=row["viaf"],
        birth=row["birth"],
        death=row["death"],
    )


@dataclass(slots=True)
class Hit:
    """A record whose heading or variant has the key looked up."""

    record: AuthorityRecord
    #: How the key matched: 'authorized', 'variant' or 'deprecated'.
    via: str


@dataclass(slots=True)
class SchemeStatus:
    name: str
    source: str | None
    snapshot_modified: str | None
    built: str
    records: int


class IndexUnavailable(Exception):
    """The index file is missing, or was built by another schema version."""


class Index:
    """Read access to a built index."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise IndexUnavailable(
                f"no index at {self.path}; build one with `lc-reconcile build`"
            )
        # check_same_thread=False: the web service shares one connection
        # across worker threads, and only ever reads through it.
        self._db = sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise IndexUnavailable(
                f"{self.path} has schema version {version}, this lc-reconcile "
                f"needs {SCHEMA_VERSION}; rebuild it with `lc-reconcile build`"
            )

    def close(self) -> None:
        self._db.close()

    def lookup(self, key: str) -> list[Hit]:
        """Every record with ``key`` as its normalized heading or a variant."""
        rows = self._db.execute(
            "SELECT a.*, l.kind AS via FROM label l JOIN authority a USING (uri) "
            "WHERE l.key = ? ORDER BY a.uri",
            (key,),
        ).fetchall()
        return [Hit(_record(row), row["via"]) for row in rows]

    def get(self, uri: str) -> AuthorityRecord | None:
        row = self._db.execute(
            "SELECT * FROM authority WHERE uri = ?", (uri,)
        ).fetchone()
        if row is None:
            return None
        record = _record(row)
        record.use_instead = self.use_instead(uri)
        return record

    def use_instead(self, uri: str) -> list[str]:
        rows = self._db.execute(
            "SELECT target FROM use_instead WHERE uri = ? ORDER BY rowid", (uri,)
        )
        return [target for (target,) in rows]

    def schemes(self) -> list[SchemeStatus]:
        rows = self._db.execute("SELECT * FROM scheme ORDER BY name").fetchall()
        return [SchemeStatus(**dict(row)) for row in rows]


class Writer:
    """Write access: bulk loading for a build, upserts for a sync."""

    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        self._db = sqlite3.connect(self.path)
        if create:
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.commit()

    def bulk_mode(self) -> None:
        """Trade crash safety for speed; only for a build into a new file."""
        self._db.execute("PRAGMA journal_mode = OFF")
        self._db.execute("PRAGMA synchronous = OFF")

    def add(self, records: Iterable[AuthorityRecord]) -> int:
        """Insert records not yet in the index, in one transaction."""
        authorities, labels, targets = [], [], []
        for record in records:
            authorities.append(_authority_row(record))
            labels.extend(label_rows(record))
            targets.extend((record.uri, target) for target in record.use_instead)
        with self._db:
            self._db.executemany(
                f"INSERT INTO authority VALUES ({', '.join('?' * 14)})", authorities
            )
            self._db.executemany("INSERT INTO label VALUES (?, ?, ?)", labels)
            self._db.executemany("INSERT INTO use_instead VALUES (?, ?)", targets)
        return len(authorities)

    def upsert(self, record: AuthorityRecord) -> None:
        """Replace whatever the index holds for this record."""
        with self._db:
            existed = self._delete(record.uri)
            self.add([record])
            if not existed:
                self._count(record.scheme, +1)

    def delete(self, uri: str) -> None:
        row = self._db.execute(
            "SELECT scheme FROM authority WHERE uri = ?", (uri,)
        ).fetchone()
        with self._db:
            if self._delete(uri) and row:
                self._count(row[0], -1)

    def _delete(self, uri: str) -> bool:
        """Remove a record's rows, saying whether it was there."""
        cursor = self._db.execute("DELETE FROM authority WHERE uri = ?", (uri,))
        for table in ("label", "use_instead"):
            self._db.execute(f"DELETE FROM {table} WHERE uri = ?", (uri,))
        return cursor.rowcount > 0

    def _count(self, scheme: str, change: int) -> None:
        self._db.execute(
            "UPDATE scheme SET records = records + ? WHERE name = ?", (change, scheme)
        )

    def create_indexes(self) -> None:
        self._db.executescript(_INDEXES)

    def record_scheme(
        self, name: str, *, source: str | None, snapshot_modified: str | None
    ) -> None:
        (count,) = self._db.execute(
            "SELECT count(*) FROM authority WHERE scheme = ?", (name,)
        ).fetchone()
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO scheme VALUES (?, ?, ?, ?, ?)",
                (
                    name,
                    source,
                    snapshot_modified,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    count,
                ),
            )

    def close(self) -> None:
        self._db.execute("PRAGMA optimize")
        self._db.close()
