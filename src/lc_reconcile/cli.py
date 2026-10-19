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
        action="append",
        metavar="DATE",
        help="when LC published a --from file, where a later sync will start. "
        "Give one for all the files, or one for each in the same order",
    )

    match = commands.add_parser("match", help="reconcile one heading")
    match.add_argument(
        "query",
        nargs="?",
        default="",
        help="the heading, e.g. 'Verne, Jules, 1828-1905'",
    )
    match.add_argument("--type", choices=sorted(TYPES))
    match.add_argument("--scheme", choices=SCHEMES)
    match.add_argument("--dates", help="dates to add to a name heading")
    match.add_argument(
        "--marc-key",
        metavar="FIELD",
        help="the heading as a MARC field, e.g. '7001 $aVerne, Jules,$d1828-1905.'",
    )
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
        dates = args.snapshot_date or [None]
        if len(dates) == 1:
            dates = dates * len(args.from_files)
        if len(dates) != len(args.from_files):
            print(
                "lc-reconcile: give one --snapshot-date, or one for each --from",
                file=sys.stderr,
            )
            return 2
        sources = [
            Source(None, path, date)
            for path, date in zip(args.from_files, dates, strict=True)
        ]
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
    if not (args.query or args.marc_key):
        print("lc-reconcile: give a heading, or --marc-key", file=sys.stderr)
        return 2
    reconciler = Reconciler(Index(args.db))
    query = Query(
        text=args.query,
        types=TYPES[args.type] if args.type else None,
        schemes=frozenset({args.scheme}) if args.scheme else None,
        dates=args.dates,
        marc_key=args.marc_key,
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
