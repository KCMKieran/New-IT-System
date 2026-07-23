#!/bin/bash
# Repeatable end-to-end test for cross-deploy asset retention
# (scripts/archive-frontend-assets.sh + the @assets_archive fallback in
# frontend/nginx.conf). Touches NO production containers — everything is
# namespaced "assets-archive-test-*", runs on a throwaway port/network, and is
# cleaned up on exit.
#
# What it proves:
#   1. Running the archive step across 5 simulated deploys keeps exactly the
#      newest 3 builds (KEEP=3) and garbage-collects chunks that only older
#      builds referenced, while shared (same-hash) chunks are stored once.
#   2. Re-running a deploy without frontend changes does NOT burn a slot.
#   3. The real frontend/nginx.conf serves current-build chunks from the image
#      root, old-build chunks from the read-only archive mount (200, immutable
#      cache headers), 404s pruned chunks, and no-caches index.html.
#
# Usage: ./scripts/test-assets-archive.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE_SCRIPT="$REPO_ROOT/scripts/archive-frontend-assets.sh"
NGINX_CONF_SRC="$REPO_ROOT/frontend/nginx.conf"

PREFIX="assets-archive-test"
FAKE_FE="$PREFIX-frontend"     # stand-in for new-it-frontend-prod
NGINX_CT="$PREFIX-nginx"
NET="$PREFIX-net"
PORT=18973

WORK="$(mktemp -d)"
ARCHIVE_ROOT="$WORK/archive"

PASS=0
FAIL=0
check() { # check <description> <actual> <expected>
  if [ "$2" = "$3" ]; then
    PASS=$((PASS + 1)); echo "  PASS: $1"
  else
    FAIL=$((FAIL + 1)); echo "  FAIL: $1 (expected [$3], got [$2])"
  fi
}

cleanup() {
  docker rm -f "$FAKE_FE" "$NGINX_CT" "$PREFIX-api" "$PREFIX-mkdocs" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# --- Build fixtures: 5 fake Vite builds. vendor-AAAA1111.js keeps the same
# hashed name+content across builds (tests the single-copy merged pool);
# page-vN-*.js is unique per build (tests retention + GC).
for v in 1 2 3 4 5; do
  mkdir -p "$WORK/build-v$v/assets"
  echo "shared vendor chunk" > "$WORK/build-v$v/assets/vendor-AAAA1111.js"
  echo "page chunk of build v$v" > "$WORK/build-v$v/assets/page-v$v-HASH$v.js"
  echo "<html>build v$v</html>" > "$WORK/build-v$v/index.html"
done

simulate_deploy() { # simulate_deploy <version>  — old container serves build vN, then archive runs
  local v="$1"
  docker rm -f "$FAKE_FE" >/dev/null 2>&1 || true
  docker run -d --name "$FAKE_FE" alpine:latest sleep 600 >/dev/null
  docker exec "$FAKE_FE" mkdir -p /usr/share/nginx/html
  docker cp "$WORK/build-v$v/assets" "$FAKE_FE:/usr/share/nginx/html/assets" >/dev/null
  "$ARCHIVE_SCRIPT" "$FAKE_FE" "$ARCHIVE_ROOT" 3
}

echo "=== Part 1: retention / pruning / dedup logic ==="

simulate_deploy 1
check "after deploy#1: 1 manifest kept" "$(find "$ARCHIVE_ROOT/manifests" -name '*.list' | wc -l)" 1
check "after deploy#1: v1 page chunk archived" "$(test -f "$ARCHIVE_ROOT/merged/page-v1-HASH1.js" && echo yes)" yes

sleep 1 # ensure a different timestamp would be generated if dedup failed
"$ARCHIVE_SCRIPT" "$FAKE_FE" "$ARCHIVE_ROOT" 3   # redeploy with NO frontend change
check "unchanged redeploy is deduped (still 1 manifest)" "$(find "$ARCHIVE_ROOT/manifests" -name '*.list' | wc -l)" 1

sleep 1; simulate_deploy 2
sleep 1; simulate_deploy 3
check "after deploy#3: 3 manifests kept" "$(find "$ARCHIVE_ROOT/manifests" -name '*.list' | wc -l)" 3
check "after deploy#3: v1 chunk still servable" "$(test -f "$ARCHIVE_ROOT/merged/page-v1-HASH1.js" && echo yes)" yes
check "shared vendor chunk stored once" "$(find "$ARCHIVE_ROOT/merged" -name 'vendor-*' | wc -l)" 1

sleep 1; simulate_deploy 4
sleep 1; simulate_deploy 5
check "after deploy#5: pruned back to 3 manifests" "$(find "$ARCHIVE_ROOT/manifests" -name '*.list' | wc -l)" 3
check "v1 chunk garbage-collected" "$(test -f "$ARCHIVE_ROOT/merged/page-v1-HASH1.js" && echo yes || echo no)" no
check "v2 chunk garbage-collected" "$(test -f "$ARCHIVE_ROOT/merged/page-v2-HASH2.js" && echo yes || echo no)" no
check "v3 chunk retained" "$(test -f "$ARCHIVE_ROOT/merged/page-v3-HASH3.js" && echo yes)" yes
check "v5 chunk retained" "$(test -f "$ARCHIVE_ROOT/merged/page-v5-HASH5.js" && echo yes)" yes
check "vendor chunk survived GC" "$(test -f "$ARCHIVE_ROOT/merged/vendor-AAAA1111.js" && echo yes)" yes

echo ""
echo "=== Part 2: real nginx.conf serves archive fallback ==="
# The current "image" is build v5; builds v3/v4 exist only in the archive.
# Dummy api/mkdocs containers give nginx resolvable upstreams for proxy_pass.
sed 's/__BACKEND_API_KEY__/test-key/g' "$NGINX_CONF_SRC" > "$WORK/default.conf"

docker network create "$NET" >/dev/null
docker run -d --name "$PREFIX-api"    --network "$NET" --network-alias api    alpine:latest sleep 600 >/dev/null
docker run -d --name "$PREFIX-mkdocs" --network "$NET" --network-alias mkdocs alpine:latest sleep 600 >/dev/null
docker run -d --name "$NGINX_CT" --network "$NET" -p "127.0.0.1:$PORT:80" \
  -v "$WORK/build-v5:/usr/share/nginx/html:ro" \
  -v "$ARCHIVE_ROOT/merged:/opt/assets-archive/assets:ro" \
  -v "$WORK/default.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:alpine >/dev/null

for _ in $(seq 1 20); do
  curl -sf -o /dev/null "http://127.0.0.1:$PORT/index.html" && break
  sleep 0.5
done

code() { curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT$1"; }

check "current build chunk (v5, from image) → 200" "$(code /assets/page-v5-HASH5.js)" 200
check "previous build chunk (v4, archive only) → 200" "$(code /assets/page-v4-HASH4.js)" 200
check "oldest kept build chunk (v3, archive only) → 200" "$(code /assets/page-v3-HASH3.js)" 200
check "pruned build chunk (v1) → 404" "$(code /assets/page-v1-HASH1.js)" 404
check "never-existed chunk → 404" "$(code /assets/nope-ZZZZ.js)" 404
check "archived chunk carries immutable Cache-Control" \
  "$(curl -sI "http://127.0.0.1:$PORT/assets/page-v4-HASH4.js" | grep -ci 'Cache-Control: public, immutable')" 1
check "index.html is no-cache" \
  "$(curl -sI "http://127.0.0.1:$PORT/index.html" | grep -ci 'no-cache, no-store, must-revalidate')" 1
# The archive lives outside the html root, so no new URL space leaks: a direct
# /assets-archive/* URL prefix-matches "location /assets", misses both roots
# (archive root would need /opt/assets-archive/assets-archive/...) and 404s.
check "archive mount not directly reachable outside /assets → 404" \
  "$(code /assets-archive/page-v4-HASH4.js)" 404

echo ""
echo "=== Result: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
