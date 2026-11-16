# lc-reconcile

Reconcile name, subject and genre/form headings against a local index of
[id.loc.gov](https://id.loc.gov) authorities.

It speaks the [Reconciliation Service API](https://www.w3.org/community/reports/reconciliation/CG-FINAL-specs-0.2-20230410/)
(version 0.2), the protocol OpenRefine uses to match the values in a column
against Wikidata and other sources of identifiers. Any tool that can use one
of those services can use this one.

The index holds LCNAF, LCSH, LCGFT, children's subject headings, LCDGT
(demographic group terms) and LCMPT (medium of performance terms), so
matching a heading doesn't send a request to id.loc.gov.

This is early work.

## Getting started

```
lc-reconcile fetch-index      # every scheme, prebuilt: a 2.7 GB download
lc-reconcile sync             # bring it up to today
lc-reconcile match "Science fiction" --type Topic
```

`fetch-index` downloads an index of every scheme, which this repository's
[Index workflow](.github/workflows/index.yml) builds, syncs and publishes
each Monday as assets of its
[`index` release](https://github.com/edsu/lc-reconcile/releases/tag/index).
It's checked against the release's manifest before it replaces the index
you have. Building the same index yourself takes much longer: LC's names
snapshot alone is 7.3 GB compressed and takes about 20 minutes to index
(see [Building an index](#building-an-index)).

The index is `lc-reconcile.sqlite` in the current directory, or
`$LC_RECONCILE_DB`, or `--db`.

## Keeping it up to date

```
lc-reconcile sync
lc-reconcile status
```

`sync` reads each scheme's
[activity stream](https://id.loc.gov/authorities/names/activitystreams/feed)
from a few days before the last sync, fetches each record that changed, and
replaces it in the index. It waits half a second between requests
(`--delay`) and saves its place after each day of changes, so it can be
stopped and run again. LCNAF changes by about 1,200 records a day, so a
daily sync takes about half an hour, and the other schemes far less. The
service can do this itself with `serve --sync-every HOURS`, and goes on
answering queries while it does.

An index more than 30 days behind isn't synced (`--max-age` raises the
limit). The quickest way to catch up is to `fetch-index` again, since the
published index is refreshed each week, and then `sync`.

`--max-minutes` stops a sync after that long, keeping what it applied, and
`--verbose` (`-v`) reports each feed page and record.

## Matching from the command line

```
lc-reconcile match "Cats" --scheme childrensSubjects
lc-reconcile match "cello" --type Medium
lc-reconcile match "Verne, Jules" --dates 1828-1905 --type PersonalName
lc-reconcile match --marc-key '7001 $aVerne, Jules,$d1828-1905,$eauthor.'
```

## The reconciliation service

```
uvx --from 'lc-reconcile[server]' lc-reconcile serve
lc-reconcile serve --port 8000 --sync-every 24
```

The reconciliation endpoint is `http://127.0.0.1:8000/reconcile`. In
OpenRefine, choose *Reconcile → Start reconciling… → Add standard
service…* and give that URL. `http://127.0.0.1:8000/` is a page for trying
it in a browser, and `GET /status` says what the index holds and how far
it's synced.

Types are the MADS kinds of heading (`PersonalName`, `Topic`, `GenreForm`
and so on). These properties sharpen a query:

- `dates`: a name's dates, when they're in a column of their own
- `marc_key`: the heading as a MARC field
- `scheme`: the schemes to search, separated by spaces: `names`,
  `subjects`, `genreForms`, `childrensSubjects`, `demographicTerms` or
  `performanceMediums`

The service also offers the optional suggest and preview services, which
OpenRefine uses when you search for a match by hand and when you hover
over a candidate:

- `GET /reconcile/suggest/entity?prefix=…` gives headings starting with
  what's been typed (at least two characters), optionally of one `type`,
  paged with `cursor`
- `GET /reconcile/suggest/type` and `GET /reconcile/suggest/property`
- `GET /reconcile/preview?id=…` gives a small HTML card for a heading

### In Docker

```
docker compose up
```

builds the image and runs the service on port 8000. The index is kept on a
volume at `/data`, not in the image. The first time the service starts
with an empty volume it fetches the prebuilt index, and it syncs on
starting and every 24 hours after. Other commands run the same way:

```
docker compose run --rm lc-reconcile status
```

`compose.yaml` sets the number of workers with `LC_RECONCILE_WORKERS`,
since a container sees all the host's CPUs. The container runs as user
999, so a host directory mounted at `/data` has to be writable by that
user.

## From Python

```python
from lc_reconcile import Index, Reconciler

reconciler = Reconciler(Index("lc-reconcile.sqlite"))
reconciler.match({"q0": {"query": "Science fiction", "type": "Topic"}})
```

## What counts as a match

Only one result is ever marked `"match": true`: the single authorized
heading of the requested type whose normalized form equals the query,
ignoring case, diacritics and most punctuation.

Failing that, a query that is the earlier established form of exactly one
heading matches that heading: `Aeroplanes` matches `Airplanes`, and
`Wang, Pao-li` matches `Wang, Baoli`. It scores 95, a little below an
authorized heading. A form LC split between headings, such as `Cookery`
(now `Cooking` and `Cookbooks`), gives both as candidates.

A marcKey (the `marc_key` property) is compared with each authority's own
marcKey subfield by subfield, ignoring relators (`$e`, `$4`), links (`$0`,
`$1`) and the source (`$2`). The tag says what kind of heading it is, so a
`710` never matches a person. A field from a vocabulary the index doesn't
hold (a 6XX with second indicator 2–6, a `$2` such as `fast` or `mesh`, or
a `653`) gets candidates but never a match.

Everything else comes back as a candidate with `"match": false`: several
authorized headings with the same label, an undifferentiated name, a
variant label, the replacement for a deprecated heading, and, when nothing
else is found, headings with similar labels from a full-text search. That
search helps with wording (`Science fiction stories`) but not with
misspellings (`Sciense fiction`).

A query without a type is matched against every kind of heading, and its
similar-heading candidates are then mostly names, which outnumber subjects
more than twenty to one. Give a type when you know it.

This is stricter than id.loc.gov's label service, which redirects
`Verne, Jules` (without dates) and variant labels straight to an authority.

### Schemes

A query searches names, subjects and genreForms unless it asks for
another. The other vocabularies repeat many of their headings, such as
children's "Cats" or LCDGT's "Women", so searching everything would turn
many matches into choices between two. A query reaches them:

- with the `scheme` property
- with a marcKey that says so: a 6XX with second indicator 1 is a
  children's heading, and a `$2` of `lcshac`, `lcgft`, `lcdgt` or `lcmpt`
  names its vocabulary (`385  $aWomen$2lcdgt`)
- with the `Medium` type, which only LCMPT has

## Building an index

```
lc-reconcile build                  # every scheme but names, a minute or so
lc-reconcile build --scheme names --scheme subjects --scheme genreForms
```

`build` replaces the whole index with the schemes you name, from LC's
[bulk snapshots](https://id.loc.gov/download/). Names isn't built by
default: indexing its 12 million records makes a 9 GB index. To build from
snapshots you've already downloaded, give each with `--from` and the date
LC published it with `--snapshot-date`. LC publishes the names snapshot
only every few weeks, so a fresh build can have days of changes to sync.

To publish an index somewhere else, `pack-index DIR` writes it as parts
and a manifest, and `fetch-index` takes the manifest's URL or path.

## Benchmarks

Against 4,154 headings sampled from LC's own BIBFRAME, matching gives LC's
choice for 96.9% of the headings LC linked, and never another heading. An
exact lookup takes about a millisecond. See [bench/README.md](bench/README.md)
for how the sample is made and the full figures.
