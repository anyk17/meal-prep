#!/bin/sh
# Rebuild and restart the day-to-day app container (deploy/docker-compose.dev.yml).
# Always records the commit as APP_REVISION: Publish needs it, and it must be pushed so the
# public site can be built from the same code.
set -eu
cd "$(dirname "$0")/.."
rev=$(git rev-parse HEAD)
if ! git branch -r --contains "$rev" | grep -q .; then
  echo "Commit $rev is not pushed; Publish would fail. Push it first." >&2
  exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Warning: uncommitted changes are built in, but the published site will use $rev." >&2
fi
APP_REVISION=$rev exec docker compose -f deploy/docker-compose.dev.yml up -d --build "$@"
