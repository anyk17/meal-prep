#!/usr/bin/env bash
# Run from WSL/Linux after installing python3-venv and Node through nvm.
set -eo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"

case "${1:-}" in
  ""|--check) ;;
  *) echo "Usage: bash scripts/setup-local.sh [--check]" >&2; exit 2 ;;
esac

# Non-interactive shells do not normally load nvm. Source it when installed.
project_nvm_dir="${NVM_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/nvm}"
if [[ ! -s "$project_nvm_dir/nvm.sh" && -s "$HOME/.nvm/nvm.sh" ]]; then
  project_nvm_dir="$HOME/.nvm"
fi
if [[ -s "$project_nvm_dir/nvm.sh" ]]; then
  source "$project_nvm_dir/nvm.sh"
  nvm use --silent
fi

if ! command -v python3 >/dev/null; then
  echo "Install Python 3.12 and its venv package in WSL/Linux first." >&2
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'

if ! command -v node >/dev/null || ! command -v npm >/dev/null; then
  echo "Install Linux Node.js first: from this directory run nvm install && nvm use." >&2
  exit 1
fi
node -e '
  const [major, minor] = process.versions.node.split(".").map(Number);
  if (process.platform !== "linux" || major !== 22 || minor < 12) {
    console.error("Use Linux Node.js 22.12+ in the 22.x series: nvm install && nvm use");
    process.exit(1);
  }
'
case "$(command -v npm)" in
  /mnt/*) echo "Use the Linux npm installed by nvm, not Windows npm." >&2; exit 1 ;;
esac

project_python="$project_root/.venv/bin/python"
if [[ "${1:-}" != --check ]]; then
  if [[ ! -x "$project_python" ]] || ! "$project_python" -m pip --version >/dev/null 2>&1; then
    if ! python3 -m venv "$project_root/.venv"; then
      echo "On Ubuntu/WSL, install python3-venv (or python3.12-venv), then rerun this script." >&2
      exit 1
    fi
  fi
  "$project_python" -m pip install --cache-dir "$project_root/.cache/pip" --upgrade pip
  "$project_python" -m pip install --cache-dir "$project_root/.cache/pip" \
    -e './planner[api,dev]' -r scripts/requirements-contract.txt
  npm --prefix site ci --cache "$project_root/.cache/npm"
fi

if [[ ! -x "$project_python" ]]; then
  echo "Project virtual environment missing. Run bash scripts/setup-local.sh first." >&2
  exit 1
fi
"$project_python" -m pip check
"$project_python" - <<'PY'
from importlib.metadata import version
from pathlib import Path
import sys

for package in ("mealplanner", "flask", "pydantic", "ortools", "pytest", "jsonschema", "openapi-spec-validator"):
    print(f"{package}: {version(package)}")
print(f"Python: {sys.version.split()[0]} ({Path(sys.executable)})")
PY
node --version
npm --version
npm --prefix site ls --depth=0
echo "Local dependencies are ready. Application tests are a separate validation step."
