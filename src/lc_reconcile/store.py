"""The SQLite index of authority records.

One file holds every scheme that has been loaded. Records are stored once in
``authority``; ``label`` maps each normalized heading and variant to the
records that carry it, and is what a lookup reads. It holds two kinds of
key: normalized labels, and heading keys made from marcKeys (see
:mod:`lc_reconcile.marckey`), which always contain ``$`` and so never
collide with a label. ``label_fts`` is a full-text index over the label
keys, for candidates when no key matches exactly.

The names snapshot has about 47 million labels for 12 million records, so
``label`` refers to records by integer id rather than by URI, and the
authority table leaves out what can be rebuilt from the URI.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from lc_reconcile.marckey import heading_key
from lc_reconcile.normalize import normalize
from lc_reconcile.records import RWO_PREFIX, AuthorityRecord

_WORDS = re.compile(r"\w+")
#: Words of a query a search uses; a long heading's first words say enough.
_MAX_WORDS = 12

#: Bumped whenever the schema changes; an index of another version is rebuilt.
SCHEMA_VERSION = 3

#: How a label belongs to its record, as stored in ``label.kind``.
VIA = ("authorized", "variant", "deprecated")
_AUTHORIZED, _VARIANT, _DEPRECATED = range(3)

_SCHEMA = """
-- rwo is the part of the RWO's URI after http://id.loc.gov/rwo/.
CREATE TABLE authority (
    id INTEGER PRIMARY KEY,
    uri TEXT NOT NULL,
    scheme TEXT NOT NULL,
    kind TEXT,
    label TEXT,
    marc_key TEXT,
    lccn TEXT,
    collections TEXT NOT NULL,
    deprecated INTEGER NOT NULL,
    deletion_note TEXT,
    rwo TEXT,
    birth TEXT,
    death TEXT
);

-- kind indexes VIA. id is declared so that label_fts can rely on it: an
-- implicit rowid may change on VACUUM.
CREATE TABLE label (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    authority INTEGER NOT NULL,
    kind INTEGER NOT NULL
);

CREATE TABLE use_instead (
    authority INTEGER NOT NULL,
    target TEXT NOT NULL
);

-- Indexes the label rows that are labels, not heading keys, and not the
-- headings of deprecated records; label_fts_* triggers keep it in step once
-- a build has filled it.
CREATE VIRTUAL TABLE label_fts USING fts5(key, content='label', content_rowid='id');

-- cursor is the date (YYYY-MM-DD) up to which changes from the scheme's
-- activity stream have been applied; synced is when a sync last finished.
CREATE TABLE scheme (
    name TEXT PRIMARY KEY,
    source TEXT,
    snapshot_modified TEXT,
    built TEXT NOT NULL,
    records INTEGER NOT NULL,
    cursor TEXT,
    synced TEXT
);
"""

# Created after a bulk load: maintaining them row by row is far slower.
_INDEXES = f"""
CREATE UNIQUE INDEX authority_uri ON authority (uri);
CREATE INDEX label_key ON label (key);
CREATE INDEX label_authority ON label (authority);
CREATE INDEX use_instead_authority ON use_instead (authority);

INSERT INTO label_fts (rowid, key)
    SELECT id, key FROM label WHERE kind != {_DEPRECATED} AND instr(key, '$') = 0;

CREATE TRIGGER label_fts_insert AFTER INSERT ON label
    WHEN new.kind != {_DEPRECATED} AND instr(new.key, '$') = 0
BEGIN
    INSERT INTO label_fts (rowid, key) VALUES (new.id, new.key);
END;

CREATE TRIGGER label_fts_delete AFTER DELETE ON label
    WHEN old.kind != {_DEPRECATED} AND instr(old.key, '$') = 0
BEGIN
    INSERT INTO label_fts (label_fts, rowid, key) VALUES ('delete', old.id, old.key);
END;
"""

#: Rows each way of searching reads before they are filtered and re-scored.
SEARCH_DEPTH = 50


def label_rows(record: AuthorityRecord) -> list[tuple[str, int]]:
    """The ``(key, kind)`` label rows for a record, one per distinct key."""
    rows: list[tuple[str, int]] = []
    seen: set[str] = set()

    def add(key: str | None, kind: int) -> None:
        if key and key not in seen:
            seen.add(key)
            rows.append((key, kind))

    def add_heading(label: str | None, marc_key: str | None, kind: int) -> None:
        if label:
            add(normalize(label), kind)
        if marc_key:
            add(heading_key(marc_key), kind)

    heading = _DEPRECATED if record.deprecated else _AUTHORIZED
    variant = _DEPRECATED if record.deprecated else _VARIANT
    add_heading(record.label, record.marc_key, heading)
    for label in record.variants:
        add_heading(label, None, variant)
    for marc_key in record.variant_keys:
        add_heading(None, marc_key, variant)
    return rows


def _authority_row(id_: int, record: AuthorityRecord) -> tuple:
    rwo = record.rwo
    return (
        id_,
        record.uri,
        record.scheme,
        record.kind,
        record.label,
        record.marc_key,
        record.lccn,
        " ".join(record.collections),
        int(record.deprecated),
        record.deletion_note,
        rwo.removeprefix(RWO_PREFIX) if rwo else None,
        record.birth,
        record.death,
    )


def _record(row: sqlite3.Row) -> AuthorityRecord:
    """A record as the index keeps it: without its variants or full types."""
    return AuthorityRecord(
        uri=row["uri"],
        scheme=row["scheme"],
        kind=row["kind"],
        label=row["label"],
        marc_key=row["marc_key"],
        lccn=row["lccn"],
        collections=row["collections"].split(),
        deprecated=bool(row["deprecated"]),
        deletion_note=row["deletion_note"],
        rwo=RWO_PREFIX + row["rwo"] if row["rwo"] else None,
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
    cursor: str | None = None
    synced: str | None = None


class IndexUnavailable(Exception):
    """The index file is missing, or was built by another schema version."""


_HIT = "SELECT a.*, l.kind AS via, l.key AS found FROM label l "
_HIT_JOIN = "JOIN authority a ON a.id = l.authority "


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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lookup(self, key: str) -> list[Hit]:
        """Every record with ``key`` as its normalized heading or a variant."""
        rows = self._db.execute(
            _HIT + _HIT_JOIN + "WHERE l.key = ? ORDER BY a.uri", (key,)
        ).fetchall()
        return [_hit(row) for row in rows]

    def search(
        self,
        key: str,
        *,
        kinds: Iterable[str] | None = None,
        schemes: Iterable[str] | None = None,
        depth: int = SEARCH_DEPTH,
    ) -> list[tuple[Hit, str]]:
        """Records with labels like ``key``, each with the label key found.

        Two searches are combined. Labels that start with ``key`` come
        first: "Verne, Jules" is the start of "Verne, Jules, 1828-1905",
        which a word search ranks below the many shorter labels that also
        mention Verne. Then labels with every word of ``key``, best first by
        the full-text rank; only if there are none is any word enough.
        """
        where, params = _filters(kinds, schemes)
        found: dict[tuple[str, str], tuple[Hit, str]] = {}

        def collect(rows: list[sqlite3.Row]) -> None:
            for row in rows:
                found.setdefault((row["uri"], row["found"]), (_hit(row), row["found"]))

        # Every key that starts with key + " ": the range up to the next
        # character after the space.
        collect(
            self._db.execute(
                _HIT + _HIT_JOIN + f"WHERE l.key >= ? AND l.key < ? "
                f"AND l.kind != {_DEPRECATED}{where} ORDER BY l.key LIMIT ?",
                [key + " ", key + "!", *params, depth],
            ).fetchall()
        )

        words = _WORDS.findall(key)[:_MAX_WORDS]
        terms = [f'"{word}"' for word in words]
        sql = (
            _HIT
            + "JOIN label_fts ON label_fts.rowid = l.id "
            + _HIT_JOIN
            + f"WHERE label_fts MATCH ?{where} ORDER BY label_fts.rank LIMIT ?"
        )
        expressions = [" ".join(terms)]
        if len(terms) > 1:
            expressions.append(" OR ".join(terms))
        for expression in expressions if terms else []:
            rows = self._db.execute(sql, [expression, *params, depth]).fetchall()
            if rows:
                collect(rows)
                break
        return list(found.values())

    def get(self, uri: str) -> AuthorityRecord | None:
        row = self._db.execute(
            "SELECT * FROM authority WHERE uri = ?", (uri,)
        ).fetchone()
        if row is None:
            return None
        record = _record(row)
        record.use_instead = self._use_instead(row["id"])
        return record

    def use_instead(self, uri: str) -> list[str]:
        row = self._db.execute(
            "SELECT id FROM authority WHERE uri = ?", (uri,)
        ).fetchone()
        return self._use_instead(row["id"]) if row else []

    def _use_instead(self, id_: int) -> list[str]:
        rows = self._db.execute(
            "SELECT target FROM use_instead WHERE authority = ? ORDER BY rowid",
            (id_,),
        )
        return [target for (target,) in rows]

    def schemes(self) -> list[SchemeStatus]:
        rows = self._db.execute("SELECT * FROM scheme ORDER BY name").fetchall()
        return [SchemeStatus(**dict(row)) for row in rows]


def _hit(row: sqlite3.Row) -> Hit:
    return Hit(_record(row), VIA[row["via"]])


def _filters(
    kinds: Iterable[str] | None, schemes: Iterable[str] | None
) -> tuple[str, list[object]]:
    """SQL conditions restricting hits to these kinds and schemes."""
    where = ""
    params: list[object] = []
    for column, values in (("kind", kinds), ("scheme", schemes)):
        if values is not None:
            values = sorted(values)
            where += f" AND a.{column} IN ({', '.join('?' * len(values))})"
            params.extend(values)
    return where, params


class Writer:
    """Write access: bulk loading for a build, upserts for a sync.

    Record ids are handed out by the writer, so only one may write to an
    index at a time.
    """

    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        self._db = sqlite3.connect(self.path)
        if create:
            self._db.executescript(_SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._db.commit()
        (last,) = self._db.execute("SELECT max(id) FROM authority").fetchone()
        self._next_id = (last or 0) + 1

    def bulk_mode(self) -> None:
        """Trade crash safety for speed; only for a build into a new file."""
        self._db.execute("PRAGMA journal_mode = OFF")
        self._db.execute("PRAGMA synchronous = OFF")

    def add(self, records: Iterable[AuthorityRecord]) -> int:
        """Insert records not yet in the index, in one transaction."""
        authorities, labels, targets = [], [], []
        for record in records:
            id_ = self._next_id
            self._next_id += 1
            authorities.append(_authority_row(id_, record))
            labels.extend((key, id_, kind) for key, kind in label_rows(record))
            targets.extend((id_, target) for target in record.use_instead)
        with self._db:
            self._db.executemany(
                f"INSERT INTO authority VALUES ({', '.join('?' * 13)})", authorities
            )
            self._db.executemany(
                "INSERT INTO label (key, authority, kind) VALUES (?, ?, ?)", labels
            )
            self._db.executemany("INSERT INTO use_instead VALUES (?, ?)", targets)
        return len(authorities)

    def upsert(self, record: AuthorityRecord) -> None:
        """Replace whatever the index holds for this record."""
        with self._db:
            existed = self._delete(record.uri) is not None
            self.add([record])
            if not existed:
                self._count(record.scheme, +1)

    def delete(self, uri: str) -> None:
        with self._db:
            scheme = self._delete(uri)
            if scheme is not None:
                self._count(scheme, -1)

    def _delete(self, uri: str) -> str | None:
        """Remove a record's rows, returning its scheme if it was there."""
        row = self._db.execute(
            "SELECT id, scheme FROM authority WHERE uri = ?", (uri,)
        ).fetchone()
        if row is None:
            return None
        id_, scheme = row
        self._db.execute("DELETE FROM authority WHERE id = ?", (id_,))
        for table in ("label", "use_instead"):
            self._db.execute(f"DELETE FROM {table} WHERE authority = ?", (id_,))
        return scheme

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
                "INSERT OR REPLACE INTO scheme VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                (
                    name,
                    source,
                    snapshot_modified,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    count,
                ),
            )

    def schemes(self) -> list[SchemeStatus]:
        cursor = self._db.execute("SELECT * FROM scheme ORDER BY name")
        columns = [column[0] for column in cursor.description]
        return [SchemeStatus(**dict(zip(columns, row, strict=True))) for row in cursor]

    def set_cursor(self, name: str, cursor: str, *, finished: bool) -> None:
        """Record that changes up to ``cursor`` have been applied."""
        synced = datetime.now(UTC).isoformat(timespec="seconds") if finished else None
        with self._db:
            self._db.execute(
                "UPDATE scheme SET cursor = ?, synced = coalesce(?, synced) "
                "WHERE name = ?",
                (cursor, synced, name),
            )

    def close(self) -> None:
        self._db.execute("PRAGMA optimize")
        self._db.close()
