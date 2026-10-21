# Production image: API-mode frontend + Flask (gunicorn) + OR-Tools. linux/amd64 (UGOS NAS, Intel N100).
# State lives in the /data volume, never in the image. Build from the repository root:
#   docker build --build-arg APP_REVISION=$(git rev-parse HEAD) -t meal-prep:dev .
# Base images are pinned by digest (multi-arch index); refresh them deliberately:
#   curl -sI -H "Authorization: Bearer $TOKEN" -H "Accept: application/vnd.oci.image.index.v1+json" \
#     https://registry-1.docker.io/v2/library/<image>/manifests/<tag>   # docker-content-digest

# --- Frontend: Node 22 (.nvmrc), locked npm install, API-mode build ---
FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS frontend
WORKDIR /build/site
COPY site/package.json site/package-lock.json ./
RUN npm ci
COPY site/ ./
RUN npm run build:api

# --- Runtime: Python 3.12 ---
FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    PYTHONPATH=/app/planner \
    MEALPLANNER_DB=/data/mealplanner.db \
    MEALPLANNER_STATIC_DIR=/app/static

# git + ssh: the publication push (phase 6b). GitHub's host keys are pinned, never fetched at run time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/github_known_hosts /etc/ssh/ssh_known_hosts

# Locked, hash-verified dependencies (planner/requirements.lock); the app runs from source via PYTHONPATH
# so db_cli's PROJECT_ROOT is /app.
COPY planner/requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --no-deps -r /tmp/requirements.lock && rm /tmp/requirements.lock

# The user must exist in /etc/passwd: ssh (the publication push) refuses to run for an unknown uid.
# Production keeps 10001; the dev Compose file builds with the developer's uid/gid so a bind-mounted
# data directory and deploy key stay readable and ssh still finds a passwd entry.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data && chown app:app /data
WORKDIR /app
COPY planner/mealplanner ./planner/mealplanner
COPY data ./data
COPY --from=frontend /build/site/dist ./static
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN python -m compileall -q /app/planner && chmod 0755 /usr/local/bin/entrypoint.sh

# The meal-prep commit this image was built from (git SHA). The publish step records it as `app-ref` in
# the public repository so its workflow builds the frontend from exactly this code. Empty = unknown, and
# publishing refuses to run. Last, so a new commit only invalidates this layer.
ARG APP_REVISION=""
ENV MEALPLANNER_APP_REVISION=$APP_REVISION

USER app:app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
ENTRYPOINT ["entrypoint.sh"]
