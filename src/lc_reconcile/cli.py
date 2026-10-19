"""The ``lc-reconcile`` command."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Sequence
from datetime import timedelta

from lc_reconcile._net import Client, FetchError
from lc_reconcile.build import SCHEMES, Source, build, default_sources
from lc_reconcile.match import TYPES, Query, Reconciler
from lc_reconcile.store import Index, IndexUnavailable
from lc_reconcile.sync import MAX_AGE, SyncError, sync

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

    sync_ = commands.add_parser(
        "sync", help="apply the changes LC has published since the last sync"
    )
    _sync_options(sync_)

    serve = commands.add_parser(
        "serve", help="run the reconciliation service (needs the server extra)"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--sync-every",
        type=float,
        metavar="HOURS",
        help="sync on starting, and then every this many hours (default: never)",
    )
    _sync_options(serve)

    commands.add_parser("status", help="say what the index holds")
    return parser


def _sync_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scheme",
        action="append",
        choices=SCHEMES,
        help="scheme to sync; repeat for more (default: every scheme in the index)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="seconds between requests to id.loc.gov (default: 0.5)",
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=MAX_AGE.days,
        metavar="DAYS",
        help="rebuild rather than sync an index further behind than this "
        f"(default: {MAX_AGE.days})",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        match args.command:
            case "build":
                return _build(args)
            case "match":
                return _match(args)
            case "sync":
                return _sync(args)
            case "serve":
                return _serve(args)
            case "status":
                return _status(args)
    except (IndexUnavailable, SyncError, FetchError) as error:
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
    with Index(args.db) as index:
        schemes = index.schemes()
    for status in schemes:
        print(f"{status.name}: {status.records:,} records", file=sys.stderr)
    print(f"built {args.db} in {time.monotonic() - started:.0f}s", file=sys.stderr)
    return 0


def _match(args: argparse.Namespace) -> int:
    if not (args.query or args.marc_key):
        print("lc-reconcile: give a heading, or --marc-key", file=sys.stderr)
        return 2
    query = Query(
        text=args.query,
        types=TYPES[args.type] if args.type else None,
        schemes=frozenset({args.scheme}) if args.scheme else None,
        dates=args.dates,
        marc_key=args.marc_key,
        limit=args.limit,
    )
    with Index(args.db) as index:
        results = [c.to_json() for c in Reconciler(index).candidates(query)]
    json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def _sync(args: argparse.Namespace) -> int:
    Index(args.db).close()  # says clearly if there is no index

    def progress(scheme: str, message: str) -> None:
        print(f"{scheme}: {message}", file=sys.stderr)

    results = sync(
        args.db,
        args.scheme,
        client=Client(delay=args.delay),
        max_age=timedelta(days=args.max_age),
        progress=progress,
    )
    for result in results:
        print(
            f"{result.scheme}: {result.updated:,} updated, {result.deleted:,} "
            f"removed, synced to {result.cursor}",
            file=sys.stderr,
        )
    return 0


def _serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from lc_reconcile.api import create_app
    except ImportError:
        print(
            "lc-reconcile: serve needs the server extra: "
            "pip install 'lc-reconcile[server]'",
            file=sys.stderr,
        )
        return 1
    app = create_app(args.db)
    if args.sync_every:
        threading.Thread(
            target=_sync_forever, args=(args,), name="sync", daemon=True
        ).start()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _sync_forever(args: argparse.Namespace) -> None:
    """Sync now, then every --sync-every hours, while the service runs."""
    while True:
        try:
            _sync(args)
        except (IndexUnavailable, SyncError, FetchError, OSError) as error:
            # Keep serving what the index has; the next sync may do better.
            print(f"lc-reconcile: sync failed: {error}", file=sys.stderr)
        time.sleep(args.sync_every * 3600)


def _status(args: argparse.Namespace) -> int:
    with Index(args.db) as index:
        schemes = index.schemes()
    for status in schemes:
        synced = (
            f", synced to {status.cursor}"
            + (f" at {status.synced}" if status.synced else " (interrupted)")
            if status.cursor
            else ", never synced"
        )
        print(
            f"{status.name}: {status.records:,} records, snapshot of "
            f"{status.snapshot_modified or 'unknown date'}, built {status.built}"
            + synced
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
