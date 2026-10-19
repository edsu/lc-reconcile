"""The ``lc-reconcile`` command."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence

from lc_reconcile.build import SCHEMES, Source, build, default_sources
from lc_reconcile.match import TYPES, Query, Reconciler
from lc_reconcile.store import Index, IndexUnavailable

DEFAULT_DB = "lc-reconcile.sqlite"

#: Built when no --scheme is given. Names is opt-in: its snapshot is 7 GB.
DEFAULT_SCHEMES = ("subjects", "genreForms")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lc-reconcile",
        description="Reconcile headings against a local index of id.loc.gov.",
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("LC_RECONCILE_DB", DEFAULT_DB),
        help=f"index file (default: $LC_RECONCILE_DB or {DEFAULT_DB})",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build_ = commands.add_parser(
        "build",
        help="build the index from LC's bulk snapshots, replacing any index there",
    )
    build_.add_argument(
        "--scheme",
        action="append",
        choices=SCHEMES,
        help=f"scheme to load; repeat for more (default: {' and '.join(DEFAULT_SCHEMES)})",
    )
    build_.add_argument(
        "--from",
        dest="from_files",
        action="append",
        metavar="FILE",
        help="read a downloaded snapshot instead of fetching one; repeat for more. "
        "Each file's scheme is read from its records",
    )
    build_.add_argument(
        "--snapshot-date",
        metavar="DATE",
        help="when LC published the --from files, where a later sync will start",
    )

    match = commands.add_parser("match", help="reconcile one heading")
    match.add_argument("query", help="the heading, e.g. 'Verne, Jules, 1828-1905'")
    match.add_argument("--type", choices=sorted(TYPES))
    match.add_argument("--scheme", choices=SCHEMES)
    match.add_argument("--dates", help="dates to add to a name heading")
    match.add_argument("--limit", type=int, default=10)

    commands.add_parser("status", help="say what the index holds")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        match args.command:
            case "build":
                return _build(args)
            case "match":
                return _match(args)
            case "status":
                return _status(args)
    except IndexUnavailable as error:
        print(f"lc-reconcile: {error}", file=sys.stderr)
        return 1
    return 2


def _build(args: argparse.Namespace) -> int:
    if args.from_files:
        if args.scheme:
            print(
                "lc-reconcile: give --scheme or --from, not both; "
                "a --from file's scheme is read from its records",
                file=sys.stderr,
            )
            return 2
        sources = [Source(None, path, args.snapshot_date) for path in args.from_files]
    else:
        sources = default_sources(args.scheme or DEFAULT_SCHEMES)

    started = time.monotonic()
    last = [started]

    def progress(scheme: str, count: int) -> None:
        now = time.monotonic()
        if now - last[0] >= 5:
            last[0] = now
            print(f"{scheme}: {count:,} records", file=sys.stderr)

    build(args.db, sources, progress=progress)
    index = Index(args.db)
    for status in index.schemes():
        print(f"{status.name}: {status.records:,} records", file=sys.stderr)
    print(f"built {args.db} in {time.monotonic() - started:.0f}s", file=sys.stderr)
    return 0


def _match(args: argparse.Namespace) -> int:
    reconciler = Reconciler(Index(args.db))
    query = Query(
        text=args.query,
        types=TYPES[args.type] if args.type else None,
        schemes=frozenset({args.scheme}) if args.scheme else None,
        dates=args.dates,
        limit=args.limit,
    )
    results = [c.to_json() for c in reconciler.candidates(query)]
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def _status(args: argparse.Namespace) -> int:
    for status in Index(args.db).schemes():
        print(
            f"{status.name}: {status.records:,} records, snapshot of "
            f"{status.snapshot_modified or 'unknown date'}, built {status.built}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
