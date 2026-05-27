#!/usr/bin/env bash
# Rebuild the MkDocs docs portal by restarting its container.
#
# Why this exists: the prod docs site runs `mkdocs serve` (a live-reload dev
# server) inside a container with docs/ bind-mounted read-only. On this host the
# watcher's inotify events don't cross the Docker bind mount, so new/edited .md
# files are NOT picked up automatically — the in-memory build stays frozen at
# container start (new files 404, edited files show the old snapshot).
# Restarting the container forces a fresh build (~3s).
#
# Invoked automatically by .githooks/post-commit and .githooks/post-merge when a
# change touches docs/ or mkdocs.yml, and by deploy.sh. Safe to run by hand too.
# See docs/operations/docs-portal.md.
set -euo pipefail

CONTAINER="new-it-mkdocs-prod"

# No-op when the prod docs container isn't running (e.g. a dev-only checkout), so
# the git hook never blocks or fails a commit on a machine without prod up.
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
  echo "[docs-refresh] $CONTAINER not running — skip."
  exit 0
fi

echo "[docs-refresh] restarting $CONTAINER to rebuild docs…"
docker restart "$CONTAINER" >/dev/null
echo "[docs-refresh] done — docs portal rebuilt."
