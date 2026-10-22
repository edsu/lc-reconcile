# lc-reconcile

Reconcile name, subject and genre/form headings against a local index of
[id.loc.gov](https://id.loc.gov) authorities.

The index is built from LC's [bulk snapshots](https://id.loc.gov/download/)
of LCNAF, LCSH, LCGFT, children's subject headings, LCDGT (demographic
group terms) and LCMPT (medium of performance terms), so matching a heading doesn't send a request to
id.loc.gov. Queries and results follow the
[Reconciliation Service API 0.2](https://www.w3.org/community/reports/reconciliation/CG-FINAL-specs-0.2-20230410/),
the protocol OpenRefine speaks.

This is early work.

## Usage

```
lc-reconcile fetch-index            # every scheme, prebuilt: a 2.7 GB download
lc-reconcile build                  # every scheme but names, a minute or so
lc-reconcile build --scheme names --scheme subjects --scheme genreForms
lc-reconcile match "Cats" --scheme childrensSubjects
lc-reconcile match "cello" --type Medium
lc-reconcile match "Science fiction" --type Topic
lc-reconcile match "Verne, Jules" --dates 1828-1905 --type PersonalName
lc-reconcile match --marc-key '7001 $aVerne, Jules,$d1828-1905,$eauthor.'
lc-reconcile sync                   # apply LC's changes since the last sync
lc-reconcile status
```

`build` replaces the whole index with the schemes you name. Names isn't
built by default: LC's names snapshot is 7.3 GB compressed, and indexing
its 12 million records takes about 20 minutes and makes a 9 GB index.
To build from snapshots you've already downloaded, give each with `--from`
and the date LC published it with `--snapshot-date`.

## A prebuilt index

`fetch-index` downloads an index of every scheme, built and synced
each week by this repository's [Index workflow](.github/workflows/index.yml)
and published as assets of its [`index` release](https://github.com/edsu/lc-reconcile/releases/tag/index).
It's one gzip stream in parts, since a release asset must be under 2 GiB,
and a manifest (`index-v4.json`) that gives each part's checksum. The
index is checked against the manifest before it replaces the one you have.
After fetching, `sync` brings it up to today, which takes minutes rather
than the hours a fresh build of names can need.

There's a manifest for each version of the index's schema, so an older
lc-reconcile goes on finding an index it can read. To publish an index
somewhere else, `pack-index DIR` writes the parts and manifest, and
`fetch-index` takes the manifest's URL or path.

## The reconciliation service

```
uvx --from 'lc-reconcile[server]' lc-reconcile serve
lc-reconcile serve --port 8000 --sync-every 24
```

`serve` runs the service, with its reconciliation endpoint at
`http://127.0.0.1:8000/reconcile`. To use it from OpenRefine, choose
*Reconcile → Start reconciling… → Add standard service…* and give that
URL. `http://127.0.0.1:8000/` is a page for trying it in a browser, using
the same requests as OpenRefine. Types are the MADS kinds of heading
(`PersonalName`, `Topic`, `GenreForm` and so on). These properties sharpen a
query:

- `dates`: a name's dates, when they're in a column of their own
- `marc_key`: the heading as a MARC field
- `scheme`: the schemes to search, separated by spaces: `names`,
  `subjects`, `genreForms`, `childrensSubjects`, `demographicTerms` or
  `performanceMediums`

The service also offers the protocol's optional suggest and preview
services, which OpenRefine uses when you search for a match by hand and
when you hover over a candidate:

- `GET /reconcile/suggest/entity?prefix=…` gives headings starting with
  what's been typed (at least two characters), headings before variants
  and shorter before longer, optionally of one `type`, paged with `cursor`
- `GET /reconcile/suggest/type` and `GET /reconcile/suggest/property`
- `GET /reconcile/preview?id=…` gives a small HTML card for a heading: its
  type, scheme, LCCN and dates, and for a deprecated heading what replaces
  it

## In Docker

```
docker compose up
```

builds the image and runs the service on port 8000. The index isn't in
the image: it's kept on a volume at `/data`, and the first time the service
starts with an empty volume it fetches the prebuilt index (a 2.7 GB
download). If there's none to fetch, it says how to build one and waits,
then starts serving as soon as an index is there. Build one from another
terminal with, for example:

```
docker compose run --rm lc-reconcile build --scheme names --scheme subjects --scheme genreForms
```

The same works for any other command (`status`, `sync`, `match …`). The
service syncs on starting and every 24 hours. A container sees all the
host's CPUs, so `compose.yaml` sets how many workers to run with
`LC_RECONCILE_WORKERS`. The container runs as user 999, so a host directory
mounted at `/data` instead of the volume has to be writable by that user.

## Schemes

A query searches names, subjects and genreForms unless it asks for
another. The other vocabularies repeat many of their headings, such as
children's "Science fiction" and "Cats", or LCDGT's "Women", so searching
everything by default would turn many matches into choices between two.
A query reaches them by asking:

- with the `scheme` property
- with a marcKey that says so: a 6XX with second indicator 1 is a
  children's heading, and a `$2` of `lcshac`, `lcgft`, `lcdgt` or `lcmpt`
  names its vocabulary (`385  $aWomen$2lcdgt`)
- with the `Medium` type, which only LCMPT has

A marcKey's field doesn't have to be a heading field: a 385 or 386 of
LCDGT terms is matched by its label. A 382 with several mediums gives each
of them as a candidate.

`GET /status` says what the index holds and how far it's synced. With
`--sync-every HOURS`, the service syncs when it starts and then on that
schedule, and goes on answering queries while it does.

## Keeping the index current

`sync` reads each scheme's
[activity stream](https://id.loc.gov/authorities/names/activitystreams/feed)
back to a few days before the last sync (or the snapshot, the first time),
fetches each record that changed, and replaces it in the index. It waits
half a second between requests (`--delay`), and saves its place after each
day of changes, so it can be stopped and run again.

A sync looks back three days because the feed's dates aren't exact: items
on a page aren't in order, and some are dated days ahead. It doesn't fetch
a record again for a change dated before the day it last fetched it, so
the look-back costs only the feed's pages. A daily sync of names says
something like this:

```
names: reading the feed back to 2026-10-18
names: 4,315 records changed since 2026-10-18 (45 feed pages)
names: 2,301 of them are up to date, fetched since they changed; 2,014 to fetch
names: applied 1,190 of 2,014, the changes up to 2026-10-21
names: applied 2,014 of 2,014, the changes up to 2026-10-22
names: 2,014 updated, 0 removed, 2,301 already up to date, synced to 2026-10-22
```

LCNAF changes by about 1,200 records a day, and a sync fetches each at
about 0.7 seconds. The feed has dates but no times, so a change dated the
day of the last sync is fetched again in case it came after, and a daily
sync fetches about two days of changes: some 2,500 records, half an hour.
LC's names snapshot is published only every few weeks: a build from one
24 days old had 26,567 changes to catch up on, about five hours.

A sync of an index more than 30 days behind is refused: rebuild it from a
newer snapshot, or raise the limit with `--max-age`. `--max-minutes` stops
a sync after that long, keeping what it applied. `--verbose` (`-v`) also
reports each feed page read and each record applied or found up to date;
otherwise a long feed, like a few weeks of names, is read in silence.

## From Python

```python
from lc_reconcile import Index, Reconciler

reconciler = Reconciler(Index("lc-reconcile.sqlite"))
reconciler.match({"q0": {"query": "Science fiction", "type": "Topic"}})
```

## What counts as a match

Only one result is ever marked `"match": true`: the single authorized
heading of the requested type whose normalized form equals the query.
Comparison ignores case, diacritics and most punctuation.

Failing that, a query that is the earlier established form of exactly one
heading matches that heading: `Aeroplanes` matches `Airplanes`, which LC
replaced it with, and `Wang, Pao-li` matches its pinyin form `Wang, Baoli`.
This is the usual case in records catalogued before a heading changed. It
scores 95, a little below an authorized heading. A form LC split between
headings, such as `Cookery` (now `Cooking` and `Cookbooks`), gives both as
candidates.

A heading can also be given as a MARC field, a marcKey (the `marc_key`
property). It's compared with each authority's own marcKey subfield by
subfield, after dropping the parts of a bibliographic field that aren't the
heading: relators (`$e`, `$4`), links (`$0`, `$1`) and the source (`$2`).
The tag says what kind of heading it is, so a `710` never matches a person.

A field that says its heading is from a vocabulary the index doesn't hold
gets LC candidates but never a match: a 6XX whose second indicator is 2
(MeSH), 3 (NAL), 4 (source not specified), 5 (Canadian) or 6 (RVM), a `$2`
other than LC's (`$2fast`, `$2mesh`), or a `653`. LC often has a heading
with the same words, but matching it would be a claim the cataloger didn't
make.

Everything else comes back as a candidate with `"match": false`:

- several authorized headings with the same label
- an undifferentiated name, which covers more than one person
- any other variant label
- the replacement for a deprecated heading
- when nothing else is found, headings with similar labels, from a
  full-text search, scored by how alike the labels are and whether the
  dates agree. A search finds labels that share words with the query, so
  it helps with wording (`Science fiction stories`) but not with
  misspellings (`Sciense fiction`)

A query without a type, and without a marcKey to supply one, is matched
against every kind of heading. Exact matches are still exact, but its
similar-heading candidates are mostly names, which outnumber subjects more
than twenty to one. Give a type (`--type Topic`, or `"type"` in the query)
when you know it.

That's stricter than id.loc.gov's label service, which redirects
`Verne, Jules` (without dates) and variant labels straight to an authority.

## Benchmarks

`bench/` holds a benchmark made from LC's own cataloguing.
`bench/headings.jsonl` is 2,054 agent, subject and genre/form headings
sampled from LC BIBFRAME records, each with the marcKey and label LC gives
it and the authority LC linked it to; `bench/extract.py` makes it from the
records. To run it against a full index:

```
uv run --extra server lc-reconcile serve --port 8000
uv run python bench/bench.py --url http://127.0.0.1:8000
```

The figures below are from 2026-10-21, on an Apple M3 Pro (12 cores,
36 GB) with every scheme in a 9.6 GB index, and Python 3.13. Each pass ran
once untimed first, so the index was in the page cache; the first lookups
in a cold file take far longer. The clients ran on the same machine.

**Accuracy.** Of the 1,674 headings LC linked to an authority, queried by
marcKey as marc-bibframe asks:

| Outcome | Headings |
|---|---|
| `match: true` for LC's own choice | 1,652 (98.7%) |
| `match: true` for another heading | 0 |
| `match: true` for the LCSH twin of a children's heading | 15 (0.9%) |
| LC's choice offered as a candidate | 4 (0.2%) |
| missed | 3 (0.2%) |

The children's headings (Cats, Bears, Mothers) are a limit of the sample,
not of matching: LC's BIBFRAME gives a subject's authority marcKey
(`150  $aCats`), which has lost the second indicator of the bibliographic
`650 _1` that says it's a children's heading. Queried by label and type,
as OpenRefine asks, the outcomes are the same.

**One request at a time**, over HTTP:

| Request | Median | 95th percentile | 99th percentile |
|---|---|---|---|
| reconcile one marcKey | 1.0 ms | 7 ms | 594 ms |
| reconcile a label LC didn't link | 1.1 ms | 621 ms | 1,265 ms |
| reconcile a batch of 50 marcKeys | 467 ms | 3.3 s | 3.6 s |
| suggest, 6-character prefix | 18 ms | 202 ms | 242 ms |
| preview | 0.7 ms | 1.2 ms | 1.4 ms |

Exact lookups take about a millisecond. About one query in a hundred falls
through to the similar-heading search and takes half a second or more,
when its words are common and its type is rare, or when no label has all
its words and the search for any of them matches millions of labels. A
batch waits for its slowest query.

**In parallel**, single marcKey queries and suggest requests per second,
by `serve --workers`:

| Workers | Reconcile, 8 clients | Reconcile, 32 clients | 99th percentile, 32 clients | Suggest, 16 clients |
|---|---|---|---|---|
| 1 | 76 | 69 | 12.0 s | 17 |
| 4 | 185 | 193 | 4.0 s | 195 |
| 8 | 243 | 255 | 3.1 s | 248 |
| 12 | 245 | 240 | 2.9 s | 290 |

One process does its matching on one core, so beyond about four clients it
only queues them. Worker processes share the index file and the page cache,
and gain little past eight on this machine.

