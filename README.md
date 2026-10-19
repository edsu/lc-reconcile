# lc-reconcile

Reconcile name, subject and genre/form headings against a local index of
[id.loc.gov](https://id.loc.gov) authorities.

The index is built from LC's [bulk snapshots](https://id.loc.gov/download/)
of LCNAF, LCSH and LCGFT, so matching a heading doesn't send a request to
id.loc.gov. Queries and results follow the
[Reconciliation Service API 0.2](https://www.w3.org/community/reports/reconciliation/CG-FINAL-specs-0.2-20230410/),
the protocol OpenRefine speaks.

This is early work. The library and the `build`, `match` and `status`
commands exist. Keeping the index current from LC's activity streams and
the web service are still to come.

## Usage

```
lc-reconcile build                  # LCSH and LCGFT, a few minutes
lc-reconcile build --scheme names --scheme subjects --scheme genreForms
lc-reconcile match "Science fiction" --type Topic
lc-reconcile match "Verne, Jules" --dates 1828-1905 --type PersonalName
lc-reconcile match --marc-key '7001 $aVerne, Jules,$d1828-1905,$eauthor.'
lc-reconcile status
```

`build` replaces the whole index with the schemes you name. Names isn't
built by default: LC's names snapshot is 7.3 GB compressed, and indexing
its 12 million records takes about 20 minutes and makes a 9 GB index.
To build from snapshots you've already downloaded, give each with `--from`
and the date LC published it with `--snapshot-date`.

From Python:

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
