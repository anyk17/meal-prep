# Meal Prep Planner

An automated meal planning system that generates optimal weekly menus based on nutritional constraints, ingredient availability, and variety rules. It includes a modern web interface for viewing your plan and shopping list.

Code layout and layer rules: see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Features

-   **Constraint-Based Planning**: Uses Google OR-Tools to ensure nutritional balance (protein targets, carb limits) and variety (no consecutive proteins, recipe repetition limits).
-   **Data-First Configuration**: Define everything in YAML (`data/recipes/*.yml`, `data/ingredients.yml`, `data/rules.yml`).
-   **Automated Shopping List**: Aggregates ingredients from the generated plan, grouping them by category (Protein, Produce, Pantry, etc.).
-   **Modern UI**: A responsive React + Tailwind CSS web application to view your weekly schedule and check off grocery items.
-   **Dark Mode**: Sleek Slate-900 based dark theme with persistent user preference.
-   **Flexible**: Supports fixed vs. optional carbs and custom dietary rules.

## Development setup (new PC)

Everything needed to continue on another machine exactly as today. More detail: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

### 1. Windows + WSL
In an Admin PowerShell: `wsl --install -d Ubuntu-24.04`, then open Ubuntu. Work inside the Linux filesystem (`~/dev`), never `/mnt/c`.

### 2. System packages, Docker, GitHub CLI
```sh
sudo apt-get update
sudo apt-get install -y git curl python3-venv docker.io docker-compose-v2 gh
sudo usermod -aG docker "$USER"     # then close and reopen WSL (or prefix docker commands with: sg docker -c "...")
gh auth login                       # HTTPS, browser login
git config --global user.name "Juan Felipe"
```

### 3. Code, Node and dependencies
```sh
mkdir -p ~/dev && cd ~/dev
gh repo clone jflopezo91/meal-prep && cd meal-prep
git checkout feature/cloud-migration
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh | bash   # reopen the terminal
nvm install && nvm use              # Node 22 from .nvmrc
bash scripts/setup-local.sh         # .venv + npm ci; --check only verifies
```

### 4. Data and the publish key (not in git)
Both live in `var/` (git-ignored). Copy them from the old machine or from a backup:

- **Database:** on the old PC, run `docker compose -f deploy/docker-compose.dev.yml exec meal-prep python -m mealplanner.entrypoints.db_cli backup --db /data/mealplanner.db --dest /data/move.db`. Then copy `var/docker-data/move.db` to the new PC as `var/docker-data/mealplanner.db`, before the first start.
- **Deploy key:** put it at `var/secrets/deploy_key` and run `chmod 600`. Alternatively, generate a fresh key with `ssh-keygen -t ed25519 -N '' -f var/secrets/deploy_key`, add its `.pub` to [meal-plan deploy keys](https://github.com/jflopezo91/meal-plan/settings/keys) with write access, and delete the old one there. Without a key, everything works except Publish.

### 5. Run it
```sh
mkdir -p var/docker-data
scripts/dev-up.sh                   # builds + starts http://127.0.0.1:8080/ (refuses an unpushed commit)
docker compose -f deploy/docker-compose.dev.yml exec meal-prep python -m mealplanner.entrypoints.db_cli user add juan   # only for a fresh database
```
Run `scripts/dev-up.sh` again after every pull or commit. Skip it and Publish uses the wrong revision, or answers 503.

### 6. Phone on the home Wi-Fi
WSL is behind Windows, so Windows forwards its Wi-Fi address to the app. Find that address with `ipconfig.exe | grep -A4 Wi-Fi` (it was `192.168.0.10`; reserve it in the router). Run once in an **Admin** PowerShell:
```powershell
netsh interface portproxy add v4tov4 listenaddress=<wifi-ip> listenport=8080 connectaddress=127.0.0.1 connectport=8080
New-NetFirewallRule -DisplayName "meal-prep 8080" -Direction Inbound -Protocol TCP -LocalPort 8080 -Profile Private -Action Allow
```
Then open `http://<wifi-ip>:8080/` on the phone. If it does not load:
- **Is the forward there?** In a normal PowerShell, `netsh interface portproxy show v4tov4` must list the rule. An empty list means the commands did not run as Administrator.
- **Is the Wi-Fi Private?** `Get-NetConnectionProfile` must show your Wi-Fi as `Private` (Settings → Network & internet → Wi-Fi → network). The firewall rule only applies on Private networks.
- **Does Windows reach the app?** `Invoke-WebRequest -UseBasicParsing http://<wifi-ip>:8080/healthz` should return 200. If only `http://127.0.0.1:8080/healthz` works, the forward is missing. If neither works, the container is down: run `scripts/dev-up.sh`.
- **Is the phone on the same network?** A guest network or mobile data cannot reach the PC.
- **Can the phone view but not log in?** The dev Compose file sets `MEALPLANNER_COOKIE_SECURE=0`. Keep it while the app is served over plain HTTP.

### 7. Browser checks with Claude Code (Playwright MCP)
Claude Code drives a headless Chromium in WSL to look at the app and debug it: clicks, screenshots, console errors. The server is declared in [.mcp.json](.mcp.json), so Claude Code offers it the first time you open the repo. Do this once per machine:
```sh
npx -y playwright@1.56.1 install-deps chromium   # system libraries (asks for sudo)
npx -y playwright@1.56.1 install chromium        # the browser itself
```
Restart Claude Code and approve the `playwright` server. Then ask it to open `http://127.0.0.1:8080/` and check something.

To test changes, use a **copy** of the data, never the real database. Copy `var/docker-data/mealplanner.db` into a scratch folder, then run a second container from the `meal-prep:dev` image on `127.0.0.1:8081` with that folder as `/data`. Create test users only there, and delete the folder afterwards.

### 8. Checks before pushing
```sh
.venv/bin/python -m pytest planner/tests -q
cd site && npm test && npm run lint
```

## Workflow: How to Generate a Weekly Plan

### Step 1: Generate the Plan
Run the planner command to solve for a new week. This will write the data directly to the UI's public folder.

```bash
# Generate a plan using a seed for reproducibility
.venv/bin/mealplanner generate-plan data site/public --seed 123
```

To validate recipe changes before generating a plan:

```bash
.venv/bin/mealplanner validate-data data
.venv/bin/mealplanner validate-recipe data/recipes/pollo_asiatico.yml
```

This command will:
1.  Read configuration from `data/`.
2.  Solve for a valid schedule satisfying all rules in `data/rules.yml`.
3.  Output `plan.json` and `shopping_list.json` directly to `site/public/`.

### Step 2: View the Plan
Start Flask from the repository root in one terminal:

```bash
.venv/bin/python -m mealplanner.entrypoints.http.app
```

In another terminal, start the frontend:

```bash
cd site
npm run dev:api
```
Open `http://127.0.0.1:5173/meal-prep/`. The current fixture is explicitly anchored to October 19–25, 2026. Restart Flask after generating new files.

For the read-only static sample without Flask, use `npm run dev:static` instead. It displays the checked-in publication example, not the latest CLI output. Setup, the container, checks and publishing are in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md); what comes next is in the [migration plan](docs/migration-to-cloud/README.md).

## Configuration

-   **`data/rules.yml`**: Define global constraints (e.g., "no carbs at dinner", protein targets per week).
-   **`data/ingredients.yml`**: Master list of ingredients with units and categories.
-   **`data/pantry.yml`**: Items you already have (excluded from shopping list).
-   **`data/recipes/*.yml`**: Individual recipe definitions (ingredients, tags, meal types).
-   **`.github/workflows/validate_recipes.yml`**: Manual workflow to validate repo data or a specific PR on demand.

## Troubleshooting

-   **Optimization Failed**: If the solver cannot find a solution, try relaxing constraints in `data/rules.yml` (e.g., increase `max_recipe_uses_per_week` or `fish_dinner_max_per_week`).
-   **UI Not Updating**: In API mode, generate into `site/public`, restart Flask, then refresh the frontend. Static mode uses a separate prepared sample bundle.
