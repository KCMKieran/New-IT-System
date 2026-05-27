#!/bin/bash
# Rebuild and restart ONLY production containers
# Dev containers (on :5173 / :8001) are NOT affected
set -e

echo "================================"
echo "  Rebuilding Production..."
echo "================================"

cd /opt/myproject/New-IT-System

# Pull latest code
git pull origin main

# Rebuild and restart prod containers only
docker compose -f docker-compose.prod.yml up -d --build

# Docs portal: `mkdocs serve` doesn't pick up host file changes (inotify doesn't
# cross the bind mount), and `up` won't recreate the pinned mkdocs image — force
# a restart so the docs site reflects the just-pulled docs/.
docker compose -f docker-compose.prod.yml restart mkdocs

# Clean up dangling images (safe, always recommended)
docker image prune -f
# Clean build cache older than 7 days (keep recent cache for fast rebuilds)
docker builder prune --filter "until=168h" -f

echo ""
docker compose -f docker-compose.prod.yml ps
echo ""
echo "================================"
echo "  Done! Prod is live on :3000"
echo "  Dev still running on :5173"
echo "================================"
