"""Time each lookup of the benchmark headings, and compare runs.

    uv run python bench/lookups.py run before
    # ... change something ...
    uv run python bench/lookups.py run after
    uv run python bench/lookups.py compare before after

looks up every heading in ``bench/headings.jsonl`` (see extract.py)
in-process, by label and type as OpenRefine asks -- which is all a heading
described as BIBFRAME has -- or, with ``--by marcKey``, by the marcKey
left over from converting MARC, as marc-bibframe asks, where there is one. It records how each was found, how long
it took, its first candidates, and how the answer compares with the
authority LC linked. A run is written to ``bench/runs/NAME.jsonl``, and its
timings by how the heading was found and its accuracy are printed.

``compare`` prints two runs' figures side by side, and every heading whose
answer changed between them, with what LC linked it to, so a change made
for speed can be seen not to change the answers -- or seen to, and whether
for the better.

How a heading was found, from the first candidate the index gives:

- ``match``: an authorized heading, the only one, so accepted.
- ``earlier``: the one heading the query gives an earlier form of, accepted.
- ``ambiguous``: several authorized headings, or an undifferentiated name.
- ``variant``: headings the query gives a variant form of.
- ``replaced``: what LC says replaces a deprecated heading.
- ``similar``: headings with similar labels, nothing exact.
- ``nothing``: no candidates at all.

The times are for one lookup at a time. The first run after the index was
last read (after a reboot, say) is slower, while the 10 GB file comes into
the page cache; run it twice and keep the second.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from bench import label_query, outcome
from lc_reconcile import Index, Query, Reconciler
from lc_reconcile.match import (
    SCORE_AUTHORIZED,
    SCORE_EARLIER,
    SCORE_REPLACEMENT,
    SCORE_VARIANT,
    Candidate,
)

HERE = Path(__file__).parent
HEADINGS = HERE / "headings.jsonl"
RUNS = HERE / "runs"

FOUND = (
    "match",
    "earlier",
    "ambiguous",
    "variant",
    "replaced",
    "similar",
    "nothing",
)

#: How an answer compares with the authority LC linked (see bench.outcome).
ANSWERS = (
    "right match",
    "LCSH twin of a children's heading",
    "first candidate",
    "other candidate",
    "wrong match",
    "missed",
)

#: How many candidates of each answer to keep, to see whether it changed.
KEPT = 3


def found(candidates: list[Candidate]) -> str:
    if not candidates:
        return "nothing"
    first = candidates[0]
    if first.match:
        return "match" if first.score == SCORE_AUTHORIZED else "earlier"
    return {
        SCORE_AUTHORIZED: "ambiguous",
        SCORE_EARLIER: "ambiguous",
        SCORE_VARIANT: "variant",
        SCORE_REPLACEMENT: "replaced",
    }.get(first.score, "similar")


def query(heading: dict, by: str) -> Query:
    if by == "marcKey":
        return Query("", marc_key=heading["marc_key"])
    return Query.from_json(label_query(heading))


def run(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        sys.exit(f"no index at {args.db}")
    headings = list(enumerate(_load(args.headings), 1))
    if args.by == "marcKey":
        # Only headings left over from MARC have one.
        headings = [(n, h) for n, h in headings if h["marc_key"]]
    reconciler = Reconciler(Index(args.db))
    rows = []
    started = time.perf_counter()
    for i, (n, heading) in enumerate(headings, 1):
        start = time.perf_counter()
        candidates = reconciler.candidates(query(heading, args.by))
        ms = 1000 * (time.perf_counter() - start)
        rows.append(
            {
                # The heading's line in the headings file, to pair runs by.
                "n": n,
                "label": heading["label"],
                "types": heading["types"],
                "marc_key": heading["marc_key"],
                "expected": heading["expected"],
                "found": found(candidates),
                "answer": outcome(candidates, heading["expected"])
                if heading["expected"]
                else None,
                "ms": round(ms, 2),
                "candidates": [
                    [c.record.uri, c.score, c.match] for c in candidates[:KEPT]
                ],
            }
        )
        if i % 250 == 0:
            elapsed = time.perf_counter() - started
            print(
                f"looked up {i:,} of {len(headings):,} ({elapsed:,.0f} s)",
                file=sys.stderr,
            )
    header = {
        "run": args.name,
        "by": args.by,
        "db": str(args.db),
        "headings": str(args.headings),
        "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": f"{platform.platform()}, {os.cpu_count()} CPUs",
    }
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{args.name}.jsonl"
    with path.open("w", encoding="utf-8") as out:
        out.write(json.dumps(header) + "\n")
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {path}\n")
    print(report({args.name: rows}))
    return 0


def compare(args: argparse.Namespace) -> int:
    before, after = _run(args.before), _run(args.after)
    print(report({args.before: before, args.after: after}))
    old = {row["n"]: row for row in before}
    changed = [
        (old[row["n"]], row)
        for row in after
        if row["n"] in old and old[row["n"]]["candidates"] != row["candidates"]
    ]
    print(f"\n{len(changed):,} of {len(after):,} answers changed")
    width = max(len(args.before), len(args.after))
    for was, now in changed[: args.show]:
        expected = (now["expected"] or "nothing").rsplit("/", 1)[-1]
        types = "+".join(now["types"])
        print(f"\n{now['label']} [{types}]  (LC linked {expected})")
        for name, row in ((args.before, was), (args.after, now)):
            first = row["candidates"][0] if row["candidates"] else None
            top = f"{first[0].rsplit('/', 1)[-1]} ({first[1]})" if first else "-"
            print(
                f"  {name:<{width}} {row['found']:<10} {row['ms']:>8.1f} ms"
                f"  {top:<20} {row['answer'] or ''}"
            )
    if len(changed) > args.show:
        print(f"\n... and {len(changed) - args.show:,} more (--show to see them)")
    return 0


def report(runs: dict[str, list[dict]]) -> str:
    """Timings by how headings were found, then accuracy, for each run."""
    width = max(len(name) for name in runs)
    header = (
        f"{'found':<10} {'run':<{width}} {'n':>6} {'median':>8} {'p90':>8}"
        f" {'p99':>8} {'max':>8} {'total':>9}"
    )
    lines = [header]
    for result in [*FOUND, "all"]:
        for name, rows in runs.items():
            ms = sorted(r["ms"] for r in rows if result in ("all", r["found"]))
            if not ms:
                continue
            lines.append(
                f"{result:<10} {name:<{width}} {len(ms):>6,}"
                f" {statistics.median(ms):>8.1f} {_at(ms, 0.9):>8.1f}"
                f" {_at(ms, 0.99):>8.1f} {ms[-1]:>8.1f} {sum(ms) / 1000:>8.1f}s"
            )
    lines.append("(times in ms, totals in seconds)")

    names = list(runs)
    counts = {
        name: Counter(r["answer"] for r in rows if r["answer"])
        for name, rows in runs.items()
    }
    lines.append("\nheadings LC linked to an authority:")
    lines.append(f"{'':<34}" + "".join(f"{name:>13}" for name in names))
    for kind in ANSWERS:
        if any(counts[name][kind] for name in names):
            lines.append(
                f"{kind:<34}" + "".join(f"{counts[n][kind]:>13,}" for n in names)
            )
    return "\n".join(lines)


def _at(ms: list[float], share: float) -> float:
    return ms[int(share * (len(ms) - 1))]


def _load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as lines:
        return [json.loads(line) for line in lines]


def _run(name: str) -> list[dict]:
    path = Path(name) if name.endswith(".jsonl") else RUNS / f"{name}.jsonl"
    return [row for row in _load(path) if "n" in row]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    timed = commands.add_parser("run", help="look up the headings and time it")
    timed.add_argument("name", help="a name for the run, e.g. baseline")
    timed.add_argument(
        "--by",
        choices=("marcKey", "label"),
        default="label",
        help="query by label and type, or by marcKey where a heading has one"
        " (default: label)",
    )
    timed.add_argument(
        "--db",
        default=os.environ.get("LC_RECONCILE_DB", "lc-reconcile.sqlite"),
        help="index file (default: $LC_RECONCILE_DB or lc-reconcile.sqlite)",
    )
    timed.add_argument("--headings", type=Path, default=HEADINGS)
    timed.set_defaults(func=run)

    diff = commands.add_parser("compare", help="compare two runs")
    diff.add_argument("before", help="a run's name, or its file")
    diff.add_argument("after")
    diff.add_argument(
        "--show", type=int, default=20, help="changed answers to show (default: 20)"
    )
    diff.set_defaults(func=compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
