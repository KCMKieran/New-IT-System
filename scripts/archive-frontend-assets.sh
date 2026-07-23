#!/bin/bash
# Archive the currently-served hashed frontend assets so browser tabs that
# still hold a pre-deploy index.html can keep lazy-loading their chunks after
# ./deploy.sh replaces the image (every build changes all chunk hashes).
#
# Called by deploy.sh BEFORE `docker compose up --build`, while the OLD
# frontend container is still running. Layout under ARCHIVE_ROOT:
#   manifests/<timestamp>.list  — file list of one archived build (one per deploy)
#   merged/                     — union of all kept builds' asset files
# merged/ is bind-mounted read-only into the prod nginx container
# (docker-compose.prod.yml) and served as a fallback for /assets/* misses
# (frontend/nginx.conf @assets_archive). Keeps the newest KEEP builds.
#
# Why manifests + one merged pool instead of per-version directories:
# Vite filenames are content-hashed, so same name == same content — merged/
# stores each chunk exactly once no matter how many builds reference it
# (unchanged vendor chunks are NOT duplicated per version).
#
# merged/ is updated IN PLACE (never dir-swapped): it may be bind-mounted by
# the running container, and replacing the directory inode would detach the
# mount until the next container recreate.
#
# Usage: archive-frontend-assets.sh [container] [archive_root] [keep]
set -euo pipefail

CONTAINER="${1:-new-it-frontend-prod}"
ARCHIVE_ROOT="${2:-$(cd "$(dirname "$0")/.." && pwd)/frontend/.assets-archive}"
KEEP="${3:-3}"

MANIFESTS="$ARCHIVE_ROOT/manifests"
MERGED="$ARCHIVE_ROOT/merged"
mkdir -p "$MANIFESTS" "$MERGED"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- 1. Extract /assets from the running container.
# Graceful skip when the container is down or has no assets dir (first-ever
# deploy, or frontend crashed): there is nothing new to preserve, but pruning
# and GC below still run so the archive stays consistent.
if docker cp "$CONTAINER":/usr/share/nginx/html/assets "$TMP/assets" 2>/dev/null; then
  (cd "$TMP/assets" && find . -type f | LC_ALL=C sort) > "$TMP/current.list"
  # Dedup: identical file list == identical build (content-hashed names), so a
  # redeploy without frontend changes must not burn one of the KEEP slots.
  NEWEST="$(find "$MANIFESTS" -maxdepth 1 -name '*.list' | LC_ALL=C sort | tail -n 1)"
  if [ -n "$NEWEST" ] && cmp -s "$NEWEST" "$TMP/current.list"; then
    echo "[assets-archive] running build already archived ($(basename "$NEWEST")) — skipping"
  else
    STAMP="$(date +%Y%m%d-%H%M%S)"
    cp -a "$TMP/assets/." "$MERGED/"
    mv "$TMP/current.list" "$MANIFESTS/$STAMP.list"
    echo "[assets-archive] archived running build as $STAMP ($(wc -l < "$MANIFESTS/$STAMP.list") files)"
  fi
else
  echo "[assets-archive] container '$CONTAINER' not running or has no /assets — nothing new to archive"
fi

# --- 2. Prune: keep only the newest KEEP manifests (timestamped names sort
# chronologically). head -n -K prints all but the last K lines, i.e. the
# expired ones; with <= KEEP manifests it prints nothing.
find "$MANIFESTS" -maxdepth 1 -name '*.list' | LC_ALL=C sort | head -n -"$KEEP" | while read -r old; do
  echo "[assets-archive] dropping expired build $(basename "$old" .list)"
  rm -f "$old"
done

# --- 3. Garbage-collect merged/: delete files referenced by no kept manifest.
find "$MANIFESTS" -maxdepth 1 -name '*.list' -exec cat {} + | LC_ALL=C sort -u > "$TMP/keep.list"
(cd "$MERGED" && find . -type f | LC_ALL=C sort) > "$TMP/have.list"
comm -23 "$TMP/have.list" "$TMP/keep.list" | while read -r stale; do
  rm -f "$MERGED/$stale"
done
# Drop now-empty subdirectories (Vite can nest e.g. fonts under assets/).
find "$MERGED" -mindepth 1 -type d -empty -delete

echo "[assets-archive] kept builds: $(find "$MANIFESTS" -maxdepth 1 -name '*.list' | wc -l), merged pool: $(find "$MERGED" -type f | wc -l) files"
