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
and published as assets of its [`index` release](https://github.com/blue-core-lod/lc-reconcile/releases/tag/index).
It's one gzip stream in parts, since a release asset must be under 2 GiB,
and a manifest (`index-v3.json`) that gives each part's checksum. The
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

`serve` runs the service at `http://127.0.0.1:8000/`. To use it from
OpenRefine, choose *Reconcile → Start reconciling… → Add standard
service…* and give that URL. Types are the MADS kinds of heading
(`PersonalName`, `Topic`, `GenreForm` and so on). These properties sharpen a
query:

- `dates`: a name's dates, when they're in a column of their own
- `marc_key`: the heading as a MARC field
- `scheme`: the schemes to search, separated by spaces: `names`,
  `subjects`, `genreForms`, `childrensSubjects`, `demographicTerms` or
  `performanceMediums`

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

LCNAF changes by about 1,200 records a day, which takes about 15 minutes to
sync. LC's names snapshot is published less often than that: a build from
one three weeks old has about 24,000 changes to catch up on, about four and
a half hours. A sync of an index more than 30 days behind is refused: rebuild
it from a newer snapshot, or raise the limit with `--max-age`.
`--max-minutes` stops a sync after that long, keeping what it applied.

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

A heading can also be given as a MARC field, a marcKey (the `marc_key`
property). It's compared with each authority's own marcKey subfield by
subfield, after dropping the parts of a bibliographic field that aren't the
heading: relators (`$e`, `$4`), links (`$0`, `$1`) and the source (`$2`).
The tag says what kind of heading it is, so a `710` never matches a person.

Everything else comes back as a candidate with `"match": false`:

- several authorized headings with the same label
- an undifferentiated name, which covers more than one person
- a variant label
- the replacement for a deprecated heading
- when nothing else is found, headings with similar labels, from a
  full-text search, scored by how alike the labels are and whether the
  dates agree. A search finds labels that share words with the query, so
  it helps with wording (`Science fiction stories`) but not with
  misspellings (`Sciense fiction`)

That's stricter than id.loc.gov's label service, which redirects
`Verne, Jules` (without dates) and variant labels straight to an authority.
