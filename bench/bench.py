"""Benchmark lc-reconcile with headings from LC's own records.

Start the service on a full index, then run the benchmark against it:

    uv run --extra server lc-reconcile serve --port 8000
    uv run python bench/bench.py --url http://127.0.0.1:8000 --db lc-reconcile.sqlite

It reports, for the headings in ``bench/headings.jsonl`` (see extract.py):

1. Accuracy: what the library answers for each heading, by marcKey (as
   marc-bibframe asks) and by label and type (as OpenRefine asks), against
   the authority LC linked it to.
2. Latency of each kind of request, in-process and over HTTP, one at a time.
3. Throughput and latency with several clients at once.

Every pass is run once beforehand and not timed, so the figures are for an
index already in the page cache; the first lookups in a cold 10 GB file are
far slower. The clients run on the same machine as the service and compete
with it for CPU.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import statistics
import sys
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from lc_reconcile import Index, Query, Reconciler
from lc_reconcile.marckey import kinds, parse

HEADINGS = Path(__file__).with_name("headings.jsonl")

#: The reconciliation type to give with a label, by the marcKey's tag.
TYPE_OF_TAG = {
    "00": "PersonalName",
    "10": "CorporateName",
    "11": "ConferenceName",
    "30": "Title",
    "50": "Topic",
    "51": "Geographic",
    "55": "GenreForm",
}


def load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as lines:
        return [json.loads(line) for line in lines]


def type_name(heading: dict) -> str | None:
    """The type OpenRefine would be given for a heading: by its tag."""
    parsed = parse(heading["marc_key"])
    if parsed is None:
        return None
    tag, _, subfields = parsed
    if tag[1:] in ("00", "10", "11") and any(code == "t" for code, _ in subfields):
        return "NameTitle"
    return TYPE_OF_TAG.get(tag[1:])


# Accuracy ---------------------------------------------------------------------


def outcome(candidates: list, expected: str | None) -> str:
    """How an answer compares with the authority LC linked."""
    matched = [c for c in candidates if c.match]
    if expected is None:
        return "matched" if matched else ("candidates" if candidates else "nothing")
    if matched:
        uri = matched[0].record.uri
        if uri == expected:
            return "right match"
        if "/childrensSubjects/" in expected and "/subjects/" in uri:
            # LC's BIBFRAME gives a subject's authority marcKey (150), not
            # the 650 with the second indicator that says it's a children's
            # heading, so nothing in the query tells the two apart.
            return "LCSH twin of a children's heading"
        return "wrong match"
    uris = [c.record.uri for c in candidates]
    if uris[:1] == [expected]:
        return "first candidate"
    if expected in uris:
        return "other candidate"
    return "missed"


def accuracy(reconciler: Reconciler, headings: list[dict]) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for way in ("marcKey", "label + type"):
        by_link: dict[str, Counter] = {}
        for heading in headings:
            if way == "marcKey":
                query = Query("", marc_key=heading["marc_key"])
            else:
                query = Query(heading["label"], types=kinds(heading["marc_key"]))
            found = reconciler.candidates(query)
            link = heading["link"] or "none"
            by_link.setdefault(link, Counter())[
                outcome(found, heading["expected"])
            ] += 1
        results[way] = {link: dict(counts) for link, counts in by_link.items()}
    return results


# Timing -----------------------------------------------------------------------


@dataclass
class Timing:
    requests: int
    seconds: float
    latencies: list[float]

    def row(self) -> dict[str, float]:
        ms = sorted(1000 * t for t in self.latencies)
        return {
            "requests": self.requests,
            "per_second": self.requests / self.seconds,
            "p50": statistics.median(ms),
            "p95": ms[int(0.95 * (len(ms) - 1))],
            "p99": ms[int(0.99 * (len(ms) - 1))],
        }


def timed(calls: list[Callable[[], object]], clients: int = 1) -> Timing:
    """Make every call, from ``clients`` threads at once, timing each."""
    latencies: list[float] = []
    lock = threading.Lock()

    def run(call: Callable[[], object]) -> None:
        start = time.perf_counter()
        call()
        elapsed = time.perf_counter() - start
        with lock:
            latencies.append(elapsed)

    start = time.perf_counter()
    with ThreadPoolExecutor(clients) as pool:
        list(pool.map(run, calls))
    return Timing(len(calls), time.perf_counter() - start, latencies)


class Client:
    """One keep-alive HTTP connection per thread, as a browser would hold."""

    def __init__(self, url: str) -> None:
        parts = urllib.parse.urlsplit(url)
        self.host, self.port = parts.hostname or "127.0.0.1", parts.port or 80
        self.root = parts.path.rstrip("/")
        self._local = threading.local()

    def _connection(self) -> http.client.HTTPConnection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = http.client.HTTPConnection(self.host, self.port, timeout=60)
            self._local.connection = connection
        return connection

    def request(self, method: str, path: str, body: str | None = None) -> bytes:
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        connection = self._connection()
        connection.request(method, self.root + path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        if response.status != 200:
            raise RuntimeError(f"{method} {path}: {response.status} {data[:200]!r}")
        return data

    def get(self, path: str, **params: str) -> bytes:
        return self.request("GET", f"{path}?{urllib.parse.urlencode(params)}")

    def reconcile(self, queries: dict[str, dict]) -> bytes:
        body = urllib.parse.urlencode({"queries": json.dumps(queries)})
        return self.request("POST", "/reconcile", body)


def marc_query(heading: dict) -> dict:
    return {"query": "", "properties": [{"pid": "marc_key", "v": heading["marc_key"]}]}


def label_query(heading: dict) -> dict:
    query: dict = {"query": heading["label"]}
    if type_ := type_name(heading):
        query["type"] = type_
    return query


def batches(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def request_kinds(client: Client, headings: list[dict]) -> dict[str, list[Callable]]:
    """The calls to time for each kind of request, over HTTP."""
    linked = [h for h in headings if h["expected"]]
    unlinked = [h for h in headings if not h["expected"] and h["label"]]
    prefixes = [h["label"][:6] for h in headings if len(h["label"]) >= 6]
    return {
        "reconcile, one marcKey": [
            (lambda h=h: client.reconcile({"q0": marc_query(h)})) for h in headings
        ],
        "reconcile, one label + type": [
            (lambda h=h: client.reconcile({"q0": label_query(h)})) for h in headings
        ],
        "reconcile, label LC didn't link": [
            (lambda h=h: client.reconcile({"q0": label_query(h)})) for h in unlinked
        ],
        "reconcile, batch of 50 marcKeys": [
            (
                lambda b=b: client.reconcile(
                    {f"q{i}": marc_query(h) for i, h in enumerate(b)}
                )
            )
            for b in batches(headings, 50)
        ],
        "suggest, 6-character prefix": [
            (lambda p=p: client.get("/reconcile/suggest/entity", prefix=p))
            for p in prefixes
        ],
        "preview": [
            (lambda h=h: client.get("/reconcile/preview", id=h["expected"]))
            for h in linked
        ],
    }


def in_process_kinds(
    reconciler: Reconciler, headings: list[dict]
) -> dict[str, list[Callable]]:
    """The same lookups made through the library, with no HTTP."""
    unlinked = [h for h in headings if not h["expected"] and h["label"]]
    return {
        "marcKey": [
            (lambda h=h: reconciler.candidates(Query("", marc_key=h["marc_key"])))
            for h in headings
        ],
        "label + type": [
            (
                lambda h=h: reconciler.candidates(
                    Query(h["label"], types=kinds(h["marc_key"]))
                )
            )
            for h in headings
        ],
        "label LC didn't link": [
            (
                lambda h=h: reconciler.candidates(
                    Query(h["label"], types=kinds(h["marc_key"]))
                )
            )
            for h in unlinked
        ],
    }


def warm(calls: Iterable[Callable[[], object]]) -> None:
    for call in calls:
        call()


# Report -----------------------------------------------------------------------


def table(header: list[str], rows: list[list[object]]) -> str:
    def cell(value: object) -> str:
        if isinstance(value, float):
            return f"{value:,.1f}"
        if isinstance(value, int):
            return f"{value:,}"
        return str(value)

    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


OUTCOMES = {
    "lc": [
        "right match",
        "wrong match",
        "LCSH twin of a children's heading",
        "first candidate",
        "other candidate",
        "missed",
    ],
    "other": ["matched", "candidates", "nothing"],
}


def report_accuracy(results: dict[str, dict]) -> str:
    parts = []
    for way, by_link in results.items():
        rows = []
        lc = by_link.get("lc", {})
        total = sum(lc.values())
        for name in OUTCOMES["lc"]:
            n = lc.get(name, 0)
            rows.append([name, n, f"{100 * n / total:.1f}%" if total else "-"])
        parts.append(f"**By {way}**, {total:,} headings LC linked to an authority:\n")
        parts.append(table(["Outcome", "Headings", "Share"], rows))
        others = []
        for link in ("fast", "other", "none"):
            counts = by_link.get(link, {})
            if counts:
                others.append(
                    [
                        {"fast": "FAST", "other": "elsewhere", "none": "nothing"}[link],
                        sum(counts.values()),
                        *(counts.get(name, 0) for name in OUTCOMES["other"]),
                    ]
                )
        if others:
            parts.append("\nHeadings LC linked to something else, or not at all:\n")
            parts.append(
                table(
                    [
                        "LC linked to",
                        "Headings",
                        "Matched",
                        "Candidates only",
                        "Nothing",
                    ],
                    others,
                )
            )
        parts.append("")
    return "\n".join(parts)


def report_timings(rows: list[tuple[str, Timing]], first: str = "Request") -> str:
    return table(
        [first, "Requests", "Per second", "p50 ms", "p95 ms", "p99 ms"],
        [
            [name, r["requests"], r["per_second"], r["p50"], r["p95"], r["p99"]]
            for name, timing in rows
            for r in [timing.row()]
        ],
    )


#: The kinds of request timed in parallel.
PARALLEL = (
    "reconcile, one marcKey",
    "reconcile, batch of 50 marcKeys",
    "suggest, 6-character prefix",
)


def machine() -> str:
    cpu = platform.processor() or platform.machine()
    if sys.platform == "darwin":
        try:
            import subprocess

            cpu = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return f"{cpu}, {os.cpu_count()} cores, Python {platform.python_version()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--db", default="lc-reconcile.sqlite")
    parser.add_argument("--headings", type=Path, default=HEADINGS)
    parser.add_argument(
        "--clients", default="1,2,4,8,16,32", help="numbers of parallel clients"
    )
    parser.add_argument(
        "--parallel-only",
        action="store_true",
        help="only time requests in parallel, e.g. to compare numbers of workers",
    )
    parser.add_argument("--json", type=Path, help="also write the results here")
    args = parser.parse_args(argv)

    headings = load(args.headings)
    client = Client(args.url)
    out: list[str] = [f"Headings: {len(headings):,}. Machine: {machine()}.\n"]
    results: dict[str, object] = {}
    timings: dict[str, dict] = {}

    kinds_ = request_kinds(client, headings)
    if not args.parallel_only:
        with Index(args.db) as index:
            reconciler = Reconciler(index)
            print("accuracy…", file=sys.stderr)
            results["accuracy"] = found = accuracy(reconciler, headings)
            out += ["## Accuracy\n", report_accuracy(found)]

            print("in-process…", file=sys.stderr)
            local = in_process_kinds(reconciler, headings)
            warm(call for calls in local.values() for call in calls)
            local_rows = [(name, timed(calls)) for name, calls in local.items()]

        print("HTTP…", file=sys.stderr)
        warm(call for calls in kinds_.values() for call in calls)
        http_rows = [(name, timed(calls)) for name, calls in kinds_.items()]
        out += [
            "## One at a time\n",
            "In-process, through the library:\n",
            report_timings(local_rows, "Lookup"),
            "\nOver HTTP, one client:\n",
            report_timings(http_rows),
            "",
        ]
        timings["in_process"] = {name: t.row() for name, t in local_rows}
        timings["http"] = {name: t.row() for name, t in http_rows}

    print("parallel…", file=sys.stderr)
    if args.parallel_only:
        warm(call for name in PARALLEL for call in kinds_[name])
    parallel: list[tuple[str, Timing]] = []
    for name in PARALLEL:
        for clients in (int(n) for n in args.clients.split(",")):
            parallel.append(
                (f"{name}, {clients} clients", timed(kinds_[name], clients))
            )
    out += ["## In parallel\n", report_timings(parallel), ""]
    timings["parallel"] = {name: t.row() for name, t in parallel}

    results["timings"] = timings
    print("\n".join(out))
    if args.json:
        args.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
