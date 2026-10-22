#!/bin/sh
# Run an lc-reconcile command: `serve` by default, or whatever is given, as in
# `docker compose run --rm lc-reconcile status`.
#
# Before serving, an index is needed. If the volume has none, fetch the
# prebuilt one. If there isn't one to fetch, say how to build one rather
# than starting a long build unasked, and wait: exiting would only have the
# container restarted to fail again. Serving starts once an index is there,
# built meanwhile with `docker compose run` or fetched on a later try.
set -e

# Stop promptly when asked, even part way through a wait.
trap 'exit 143' TERM INT

# How often to look for an index built meanwhile, and to try fetching again.
LOOK_EVERY=10
FETCH_EVERY=3600

if [ "${1:-serve}" = serve ] && [ ! -e "$LC_RECONCILE_DB" ]; then
    echo "lc-reconcile: no index at $LC_RECONCILE_DB; fetching the prebuilt one" >&2
    if ! lc-reconcile fetch-index; then
        cat >&2 <<MESSAGE
lc-reconcile: couldn't fetch a prebuilt index. Build one instead, which for
names takes about 20 minutes and a 7.3 GB download:

    docker compose run --rm lc-reconcile build --scheme names --scheme subjects \\
        --scheme genreForms --scheme childrensSubjects \\
        --scheme demographicTerms --scheme performanceMediums

or, for a quick start without names: docker compose run --rm lc-reconcile build

Waiting for an index: serving starts once one is built, and fetching is
tried again every hour.
MESSAGE
        waited=0
        until [ -e "$LC_RECONCILE_DB" ]; do
            sleep "$LOOK_EVERY" &
            wait $!
            waited=$((waited + LOOK_EVERY))
            if [ "$waited" -ge "$FETCH_EVERY" ]; then
                waited=0
                lc-reconcile fetch-index 2>/dev/null || true
            fi
        done
        echo "lc-reconcile: found an index at $LC_RECONCILE_DB" >&2
    fi
fi

exec lc-reconcile "$@"
