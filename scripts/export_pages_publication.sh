#!/usr/bin/env bash
# Export selected months into publication/ for the Pages build, keeping the three newest publications.
# Usage: scripts/export_pages_publication.sh 2026-10,2026-11 [--db var/mealplanner.db]
# Then: git add publication && git commit && git push, run "Build Frontend", then "Deploy to Pages" with its run ID.
set -euo pipefail
cd "$(dirname "$0")/.."
months="${1:?usage: $0 YYYY-MM[,YYYY-MM...] [--db PATH]}"; shift
.venv/bin/python -m mealplanner.entrypoints.db_cli export --months "$months" --out publication "$@"
# Publication ids start with a UTC timestamp, so name order is age order. Pinned browsers on a
# removed publication get publication_expired and are offered a refresh.
ls -1d publication/publications/*/ | sort | head -n -3 | xargs -r rm -rf
