#!/bin/sh
# Container entrypoint: bring the schema up to head before starting
# whatever command this container was asked to run, so a fresh database
# self-bootstraps instead of failing every cycle forever (sync_catalog's
# queries would hit missing tables, get caught by main_loop's per-cycle
# exception handler, and just retry silently with no schema ever getting
# created).
#
# With no args (the image's default CMD) this runs the scheduler loop.
# `docker compose run --rm bihr-connector <command> ...` (per README) still
# overrides it with any other command -- migrations run first either way.
set -e

alembic upgrade head

if [ "$#" -eq 0 ]; then
    exec python -m bihr_connector.cli run-scheduler
fi
exec "$@"
