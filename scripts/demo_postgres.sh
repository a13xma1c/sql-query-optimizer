#!/usr/bin/env sh
# Create the demo tables in a PostgreSQL database.
#   scripts/demo_postgres.sh postgresql+psycopg2://postgres@localhost:5432/demo [--replace|--drop] [--scale 0.5]
set -eu
cd "$(dirname "$0")/.."
exec uv run python -m sqlanalyzer.demo postgres "$@"
