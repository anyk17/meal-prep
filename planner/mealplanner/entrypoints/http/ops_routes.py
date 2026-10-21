"""Container-facing routes: health check and same-origin serving of the built frontend.

Wiring only. Static serving is opt-in (MEALPLANNER_STATIC_DIR) so the Vite dev flow, where
the frontend and API are separate processes, is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FRONTEND_BASE_PATH = "/meal-prep"  # must match `base` in site/vite.config.js
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


def register_health_route(app: Any) -> None:
    from flask import jsonify

    @app.get("/healthz")
    def healthz():
        # Liveness only: touches no database, so a busy solver never fails the check.
        response = jsonify({"status": "ok"})
        response.headers["Cache-Control"] = "no-store"
        return response


def register_static_routes(app: Any, static_dir: Path | str) -> None:
    """Serve the API-mode build at FRONTEND_BASE_PATH with an index.html fallback."""
    from flask import abort, redirect, send_file
    from werkzeug.security import safe_join

    root = Path(static_dir).resolve()
    if not (root / "index.html").is_file():
        raise RuntimeError(f"MEALPLANNER_STATIC_DIR has no index.html: {root}")

    def index():
        response = send_file(root / "index.html", mimetype="text/html", max_age=0)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def root_redirect():
        return redirect(f"{FRONTEND_BASE_PATH}/", code=302)

    @app.get(f"{FRONTEND_BASE_PATH}/")
    def spa_index():
        return index()

    @app.get(f"{FRONTEND_BASE_PATH}/<path:asset_path>")
    def spa_file(asset_path: str):
        candidate = safe_join(str(root), asset_path)
        if candidate is not None and Path(candidate).is_file():
            response = send_file(candidate, max_age=0)
            # Vite emits content-hashed names only under assets/.
            if asset_path.startswith("assets/"):
                response.headers["Cache-Control"] = IMMUTABLE_CACHE
            else:
                response.headers["Cache-Control"] = "no-cache"
            return response
        if "." in asset_path.rsplit("/", 1)[-1]:
            abort(404)  # a missing file is a 404, not the app shell
        return index()  # client-side route
