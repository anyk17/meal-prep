#!/bin/sh
# Container start: create or migrate the database in /data, then run gunicorn as PID 1.
set -eu

db="${MEALPLANNER_DB:-/data/mealplanner.db}"
cli="python -m mealplanner.entrypoints.db_cli"

if [ ! -f "$db" ]; then
    # Fresh volume: creates the schema and loads the planner inputs shipped in the image.
    $cli import --db "$db"
else
    # Existing database: apply pending schema migrations only. Changed recipe inputs in a new image
    # are never applied silently; run `db_cli import --replace-inputs` deliberately.
    $cli migrate --db "$db"
fi

# One worker: the solver gate (MEALPLANNER_MAX_CONCURRENT_GENERATIONS) is per process and the solver
# is CPU-bound. Threads keep reads responsive during a solve. The timeout covers a month fill.
exec gunicorn "mealplanner.entrypoints.http.app:create_app()" \
    --bind 0.0.0.0:8080 \
    --workers "${WEB_WORKERS:-1}" --threads "${WEB_THREADS:-4}" \
    --timeout "${WEB_TIMEOUT:-300}" \
    --worker-tmp-dir /dev/shm \
    --access-logfile - --error-logfile -
