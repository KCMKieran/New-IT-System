#!/usr/bin/env bash
# Validate frontend/nginx.conf before a rebuild. A syntax error would still
# produce a successful image build, then crash-loop the container and take
# the whole site down — this catches it first.
#
# Why a throwaway container and not `RUN nginx -t` inside Dockerfile.prod:
# nginx resolves the literal upstream hostname `api` (proxy_pass
# http://api:8001) at config-PARSE time. During `docker build` there is no
# compose network, so `nginx -t` would fail with
#   host not found in upstream "api"
# and break every build. Joining the compose network here makes `api`
# resolvable, so the check exercises the real configuration.
#
# /var/log/nginx-audit is created inside the throwaway container because
# `nginx -t` actually open()s every access_log target; in prod that directory
# comes from the ./logs/nginx-prod bind mount.
#
# The API key placeholder is replaced with a dummy value: Dockerfile.prod does
# the real substitution at build time, and an unsubstituted __BACKEND_API_KEY__
# is itself valid nginx syntax, so nothing is lost by using a dummy.
set -euo pipefail
cd "$(dirname "$0")/.."

docker run --rm --network new-it-system_default \
  -v "$PWD/frontend/nginx.conf:/tmp/default.conf:ro" \
  nginx:alpine sh -c '
    cp /tmp/default.conf /etc/nginx/conf.d/default.conf
    sed -i "s|__BACKEND_API_KEY__|dummy|g" /etc/nginx/conf.d/default.conf
    mkdir -p /var/log/nginx-audit
    nginx -t'

echo "nginx.conf OK"
