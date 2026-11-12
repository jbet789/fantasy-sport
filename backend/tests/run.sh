#!/bin/bash
# Usage: tests/run.sh  -> builds a fresh database from the migrations and runs every test script.
set -e
DB=${DB:-fl_test}
HERE="$(cd "$(dirname "$0")/.." && pwd)"
export PGHOST=${PGHOST:-/tmp/pgtest} PGPORT=${PGPORT:-5499} PGUSER=${PGUSER:-postgres}
psql -q -d postgres -c "drop database if exists ${DB}" -c "create database ${DB}"
psql -q -v ON_ERROR_STOP=1 -d ${DB} -f "$HERE/tests/stubs.sql"
for f in "$HERE"/supabase/migrations/*.sql; do echo "-- $(basename $f)"; psql -q -v ON_ERROR_STOP=1 -d ${DB} -f "$f"; done
psql -q -v ON_ERROR_STOP=1 -d ${DB} -f "$HERE/supabase/seed.sql"
psql -q -v ON_ERROR_STOP=1 -d ${DB} -f "$HERE/tests/helpers.sql"
