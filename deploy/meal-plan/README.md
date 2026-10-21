# meal-plan publication repository

Template for the public `jflopezo91/meal-plan` repository. Copy `.github/workflows/pages.yml` there.

Layout of the repository:

- `publication/`: `index.json` and `publications/<id>/...`, written by the meal-prep home app (Publish button) or `scripts/export_pages_publication.sh`.
- `app-ref`: one line, the 40-character commit SHA of the public `jflopezo91/meal-prep` repository whose frontend builds the site.
- `.github/workflows/pages.yml`: on every push to `main` (or manually), validates `app-ref` and the active publication, builds the frontend with `VITE_BASE_PATH=/meal-plan/`, and deploys to GitHub Pages.

One-time setup:

1. Settings, Pages, Source: **GitHub Actions**.
2. Keep the repository public. Everything under `publication/` is readable by anyone with the link and stays in git history even after a later publication replaces it.
3. Add a deploy key (write access) for the home app; no secrets are needed by the workflow itself.
4. Commit an initial `app-ref` (`git -C <meal-prep> rev-parse HEAD`). To upgrade the frontend, change `app-ref` and push.

The workflow reads no secrets, checks meal-prep out without credentials, and pins every action to a commit SHA. The site is served at `https://jflopezo91.github.io/meal-plan/`.
