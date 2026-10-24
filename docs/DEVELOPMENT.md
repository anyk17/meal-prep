# Development and operations

How to set up, run, check and publish. Design: [ARCHITECTURE.md](ARCHITECTURE.md). What comes next: [migration plan](migration-to-cloud/README.md).

## Setup (WSL/Linux)

Python 3.12 with `venv`, Linux Node 22 (≥ 22.12, selected by [.nvmrc](../.nvmrc)) through [nvm](https://github.com/nvm-sh/nvm#installing-and-updating). Use Linux Node/npm inside WSL, never Windows executables.

```sh
sudo apt-get install -y python3-venv curl git
cd /home/julopez/dev/meal-prep
nvm install && nvm use
bash scripts/setup-local.sh          # .venv, planner + dev extras, npm ci; --check only verifies
```

## Run the home app (container)

The day-to-day way to run it, and the only one set up to publish. Needs Docker, with your user in the `docker` group.

```sh
mkdir -p var/docker-data
scripts/dev-up.sh                                        # build + start, http://127.0.0.1:8080/
docker compose -f deploy/docker-compose.dev.yml down
```

`dev-up.sh` sets `APP_REVISION` to the current commit and refuses an unpushed one: Publish tells the public site to render with that commit's frontend, so it must exist on GitHub. A plain `docker compose up --build` leaves the revision empty, and Publish then answers `503 publishing_disabled`.

- **Data:** `var/docker-data/` (git-ignored). The first start imports the planner inputs shipped in the image; later starts only run `db_cli migrate`. Newer recipe inputs are not applied by themselves: run `db_cli import --replace-inputs` when you want them.
- **Container:** gunicorn with 1 worker and 4 threads (`WEB_WORKERS`, `WEB_THREADS`, `WEB_TIMEOUT`). It runs non-root with a read-only root filesystem and no capabilities. `GET /healthz` is the liveness check. The dev Compose file runs as your uid/gid (`DEV_UID`/`DEV_GID`), so the bind-mounted data and deploy key stay yours.
- **Users:** viewing is open, while changes and publishing need a login. The password comes from a prompt or stdin, never from an argument.

  ```sh
  docker compose -f deploy/docker-compose.dev.yml exec meal-prep python -m mealplanner.entrypoints.db_cli user add <name>
  # also: user passwd|disable|enable|logout-all|remove <name>, user list, --password-stdin
  ```
- **Backup/restore:** `db_cli backup` / `db_cli restore`. Take a backup before a schema change, because `migrate` refuses a database newer than the image supports.

### Use it from a phone (home Wi-Fi)

WSL sits behind Windows, so Windows forwards its Wi-Fi address to the app. Run this once in an Admin PowerShell (`192.168.0.10` is this PC's Wi-Fi address; reserve it in the router):

```powershell
netsh interface portproxy add v4tov4 listenaddress=192.168.0.10 listenport=8080 connectaddress=127.0.0.1 connectport=8080
New-NetFirewallRule -DisplayName "meal-prep 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Profile Private -Action Allow
```

Then open `http://192.168.0.10:8080/` on the phone. The Wi-Fi must be a **Private** network in Windows. The dev Compose file sets `MEALPLANNER_COOKIE_SECURE=0`, because a secure cookie would block login over plain HTTP. To undo: `netsh interface portproxy delete v4tov4 listenaddress=192.168.0.10 listenport=8080` and `Remove-NetFirewallRule -DisplayName "meal-prep 8080"`.

## Develop the code (Vite + Flask)

```sh
.venv/bin/python -m mealplanner.entrypoints.db_cli import --fixture          # once: var/mealplanner.db
MEALPLANNER_DB=var/mealplanner.db .venv/bin/python -m mealplanner.entrypoints.http.app   # :5000
cd site && npm run dev:api                                                   # http://127.0.0.1:5173/meal-prep/
cd site && npm run dev:static -- --port 5174                                 # read-only sample, no Flask
```

Without `MEALPLANNER_DB`, the API serves the JSON snapshot read-only and answers writes with `403 read_only`. Run `db_cli migrate` on an existing database before you serve it: the API never migrates, and an unmigrated database answers `503 source_unavailable`. To preview a published bundle, run `db_cli export --months 2026-10 --out <dir>`, then `VITE_PUBLICATION_DIR=<dir> npm run dev:static`.

## Checks

```sh
.venv/bin/python -m pytest planner/tests -q
.venv/bin/python scripts/validate_api_contract.py --require-openapi-validator --self-test
.venv/bin/python scripts/validate_publication_contract.py --self-test
cd site && npm test && npm run lint && npm run build:api && npm run build:pages
```

`planner/tests/test_architecture.py` enforces the layer rules. The API contract lives in [openapi.yaml](migration-to-cloud/openapi.yaml) and the public bundle schema in [publication.schema.json](migration-to-cloud/publication.schema.json); both have examples next to them, and the validators and tests read them in place.

## Publish

The home app pushes `publication/` and `app-ref` to [`jflopezo91/meal-plan`](https://github.com/jflopezo91/meal-plan). That repo's workflow builds the public site at https://jflopezo91.github.io/meal-plan/. In the app, open Publish, choose months, check the preview, and press Publish. A job is shown as `live` only once GitHub confirms the deployment. A failed job leaves the previous public version in place.

**Deploy key:** an SSH key with write access to `meal-plan` only. The private half is a file mounted at `/run/secrets/deploy_key`, never an environment variable or anything in the image. It must be owned by the container's uid and set to `chmod 600`:

- dev: `var/secrets/deploy_key`, owned by your uid
- NAS: `/volume1/docker/meal-prep/secrets/deploy_key`, owned by uid 10001

To test without GitHub, set `MEALPLANNER_PUBLISH_REPO` to a local bare repository that has one commit on `main`.

```sh
curl -s 'http://127.0.0.1:8080/api/v1/publishing/preview?months=2026-10'
curl -s -X POST -H 'Content-Type: application/json' -d '{"months":["2026-10"]}' http://127.0.0.1:8080/api/v1/publishing/jobs
```

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `MEALPLANNER_DB` | unset (read-only JSON) | SQLite file |
| `MEALPLANNER_TIMEZONE` | `America/Bogota` | Week dates and "today" |
| `MEALPLANNER_SOLVER_TIMEOUT_SECONDS` | 30 | Solver limit per week |
| `MEALPLANNER_MAX_CONCURRENT_GENERATIONS` | 1 | Extra requests get `503 generation_busy` |
| `MEALPLANNER_STATIC_DIR` | set by the image | Serve the built frontend |
| `MEALPLANNER_COOKIE_SECURE` | 1 | Set to `0` for plain HTTP on the LAN |
| `MEALPLANNER_TRUSTED_PROXIES` | 0 | Only behind a reverse proxy (e.g. Caddy) |
| `MEALPLANNER_PUBLISH_REPO` | unset (publishing off) | `git@github.com:jflopezo91/meal-plan.git` |
| `MEALPLANNER_PUBLISH_KEY` | `/run/secrets/deploy_key` | Deploy key file |
| `MEALPLANNER_PUBLISH_KNOWN_HOSTS` | `/etc/ssh/ssh_known_hosts` | GitHub host keys (`deploy/github_known_hosts`) |
| `MEALPLANNER_PUBLISH_WORKDIR` | `<db dir>/publish-repo` | Local clone of `meal-plan` |
| `MEALPLANNER_PUBLIC_SITE_URL` | the Pages URL | Shown after publishing |
| `MEALPLANNER_APP_REVISION` | image build arg, else `git rev-parse HEAD` | Commit the public site renders with |

Python dependencies are hash-locked in `planner/requirements.lock`. To regenerate it, run `pip-compile --generate-hashes --strip-extras --extra api --extra server -o requirements.lock pyproject.toml` in `planner/`. If GitHub rotates its SSH keys, refresh `deploy/github_known_hosts` from `api.github.com/meta`.
