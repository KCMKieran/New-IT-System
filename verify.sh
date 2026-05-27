#!/usr/bin/env bash
#
# verify.sh — the single canonical red/green gate for the KCM IT System.
#
# This is the "oracle" that any autonomous / headless development loop trusts
# to decide whether a change is safe to propose. Exit code is the machine
# signal: 0 = every hard gate is green, non-zero = at least one failed.
# A human-readable summary prints at the end; the final line is
# `VERIFY: PASS` or `VERIFY: FAIL` for easy grepping.
#
# Hard gates (must pass — they gate the exit code):
#   1. backend  pytest   — 131 tests, the strongest correctness signal here
#   2. frontend tsc -b   — type check (build was silently broken before this)
#   3. frontend vitest   — unit tests (commission math, filter persistence)
#
# Advisory (reported but does NOT gate exit code):
#   - frontend eslint    — ~337 pre-existing errors (mostly no-explicit-any).
#       Can't be a hard gate until that debt is paid down. Future hardening:
#       turn this into a *delta* gate (fail only if the current diff ADDS new
#       lint errors vs the base branch) — that's the right autonomy guardrail.
#
# Usage:
#   ./verify.sh            # run every gate
#   ./verify.sh --quiet    # only print the per-gate PASS/FAIL + final verdict
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1

# Pick the backend python: prefer the project venv, fall back to PATH.
PYTEST="$ROOT/backend/.venv/bin/pytest"
[[ -x "$PYTEST" ]] || PYTEST="pytest"

FAILED_GATES=()

# run_gate <label> <workdir> <command...>
#   Runs a hard gate, records failure, streams output unless --quiet.
run_gate() {
  local label="$1" workdir="$2"; shift 2
  printf '\n\033[1m── %s ──\033[0m\n' "$label"
  local start; start=$(date +%s)
  local rc=0
  if [[ $QUIET -eq 1 ]]; then
    ( cd "$workdir" && "$@" ) >/dev/null 2>&1 || rc=$?
  else
    ( cd "$workdir" && "$@" ) || rc=$?
  fi
  local dur=$(( $(date +%s) - start ))
  if [[ $rc -eq 0 ]]; then
    printf '\033[32m✓ %s\033[0m (%ds)\n' "$label" "$dur"
  else
    printf '\033[31m✗ %s\033[0m (%ds, exit %d)\n' "$label" "$dur" "$rc"
    FAILED_GATES+=("$label")
  fi
}

# ── Hard gates ─────────────────────────────────────────────────────────────
run_gate "backend pytest"  "$ROOT/backend"  "$PYTEST" tests/ -q
run_gate "frontend tsc"    "$ROOT/frontend" npx tsc -b
run_gate "frontend vitest" "$ROOT/frontend" npm test --silent

# ── Advisory: lint (never gates the verdict) ─────────────────────────────────
printf '\n\033[1m── frontend eslint (advisory) ──\033[0m\n'
lint_out="$( cd "$ROOT/frontend" && npm run lint 2>&1 )" || true
lint_summary="$(printf '%s\n' "$lint_out" | grep -E '✖ [0-9]+ problem' | tail -1)"
if [[ -n "$lint_summary" ]]; then
  printf '\033[33m⚠ %s\033[0m (advisory — does not block)\n' "$lint_summary"
else
  printf '\033[32m✓ lint clean\033[0m\n'
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
printf '\n────────────────────────────────────────\n'
if [[ ${#FAILED_GATES[@]} -eq 0 ]]; then
  printf '\033[32mVERIFY: PASS\033[0m\n'
  exit 0
else
  printf '\033[31mVERIFY: FAIL\033[0m — %s\n' "${FAILED_GATES[*]}"
  exit 1
fi
