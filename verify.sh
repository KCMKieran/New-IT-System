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
#   1. backend  pytest   — ~1349 tests, the strongest correctness signal here
#   2. frontend tsc -b   — type check (build was silently broken before this)
#   3. frontend vitest   — unit tests (commission math, filter persistence)
#
# Advisory (reported but does NOT gate exit code):
#   - frontend eslint    — ~337 pre-existing errors (mostly no-explicit-any).
#       Can't be a hard gate until that debt is paid down. Future hardening:
#       turn this into a *delta* gate (fail only if the current diff ADDS new
#       lint errors vs the base branch) — that's the right autonomy guardrail.
#
# Speed: tests marked `slow` are deselected by default (2026-08-17). One test —
# case_metrics' idempotency check — runs the real nightly batch job twice over
# the live ~1,050-client roster and takes ~11 minutes, which is 88% of the whole
# gate and made people avoid running verify.sh at all. Pass --full to include
# it. The deselection is announced in the output on every run: a gate that
# quietly covers less than it used to is worse than a slow one.
#
# Usage:
#   ./verify.sh            # every gate, minus `slow` tests
#   ./verify.sh --full     # every gate, including `slow` tests (adds ~11 min)
#   ./verify.sh --quiet    # only the per-gate PASS/FAIL + final verdict
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QUIET=0
FULL=0
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --full)  FULL=1 ;;
    *) printf 'unknown option: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

# Deselect by marker, not by --ignore path: a mistyped --ignore path is accepted
# silently by pytest and ignores nothing, which is exactly how an 11-minute run
# got mistaken for a 90-second one on 2026-08-17.
if [[ $FULL -eq 1 ]]; then
  PYTEST_SELECT=()
else
  PYTEST_SELECT=(-m "not slow")
fi

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
run_gate "backend pytest"  "$ROOT/backend"  "$PYTEST" tests/ -q "${PYTEST_SELECT[@]}"
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

# ── Advisory: fxbackoffice schema-context freshness (OPT-0036, never gates) ──
FXBO_INDEX="$ROOT/.cursor/skills/database-context/fxbackoffice/_index.md"
if [[ -f "$FXBO_INDEX" ]]; then
  gen_date="$(grep -oE '生成 [0-9]{4}-[0-9]{2}-[0-9]{2}' "$FXBO_INDEX" | grep -oE '[0-9-]+' || true)"
  if [[ -n "$gen_date" ]]; then
    age_days=$(( ( $(date +%s) - $(date -d "$gen_date" +%s) ) / 86400 ))
    if (( age_days > 30 )); then
      printf '\033[33m⚠ fxbackoffice schema context is %d days old (gen %s) — re-run backend/scripts/dump_fxbackoffice_schema.py\033[0m (advisory)\n' "$age_days" "$gen_date"
    fi
  fi
fi

# ── Coverage disclosure ──────────────────────────────────────────────────────
# Printed on EVERY run, including --quiet. An autonomous loop trusts this
# script's exit code as an oracle; if the default run covers less than the
# full suite, that has to be visible in the same breath as the verdict rather
# than buried in a comment nobody reads.
printf '\n────────────────────────────────────────\n'
if [[ $FULL -eq 1 ]]; then
  printf '\033[32mcoverage: FULL\033[0m — `slow` tests included\n'
else
  printf '\033[33mcoverage: tests marked `slow` were NOT run\033[0m (live cloud-DB, ~11 min)\n'
  printf '           run \033[1m./verify.sh --full\033[0m before trusting this as a release gate\n'
fi

# ── Verdict ──────────────────────────────────────────────────────────────────
if [[ ${#FAILED_GATES[@]} -eq 0 ]]; then
  printf '\033[32mVERIFY: PASS\033[0m\n'
  exit 0
else
  printf '\033[31mVERIFY: FAIL\033[0m — %s\n' "${FAILED_GATES[*]}"
  exit 1
fi
