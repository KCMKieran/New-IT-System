#!/bin/bash
# Audit-trail health check — design docs/architecture/audit-log-design.md §D6.4.
#
# WHY THIS EXISTS: record_audit() swallows its own sqlite errors on purpose (an
# audit failure must not roll back the privileged change a manager just
# confirmed). The consequence is the nastiest failure mode an audit system has —
# the business write succeeds, the API answers 200, and the row is simply not
# there. "Nothing in the audit log" and "nobody ever did it" look identical.
# The ONLY surviving evidence is a line in the application log, so something has
# to go and read those lines. That is this script.
#
# Three grep tokens are a stable contract with the code. Do not rename them:
#   AUDIT_WRITE_FAILED  services/auth_service.py + core/audit.py  (row was lost)
#   AUDIT_ANONYMOUS     core/audit.py                            (no identity)
#   AUDIT_MISSING       core/audit_missing_middleware.py         (never wired up)
#
# Usage:
#   backend/scripts/check_audit_health.sh            # exit 0 healthy, 1 alerts
#   LOGDIR=/tmp/l DB=/tmp/users.db  …/check_audit_health.sh   # for tests
#
# Intended caller: /opt/myproject/morning-digest (daily 08:00 HK).

set -uo pipefail

LOGDIR="${LOGDIR:-/opt/myproject/New-IT-System/backend/logs}"
DB="${DB:-/opt/myproject/New-IT-System/backend/data/users.db}"
# Alert when the table grows faster than a human plausibly could. Design §D5.5:
# the real risk is not slow growth, it is somebody with a valid session hammering
# a write endpoint; normal is well under 20 rows/day.
DAILY_ROW_ALERT="${DAILY_ROW_ALERT:-200}"

ALERT=""

# Count a token across the current log and any same-day rotations. `grep -c`
# exits 1 when it matches nothing, which under `set -e` would end the script
# early and report "healthy" — hence `|| true` and the ${n:-0} default.
count_token() {
    local token="$1" n
    n=$(cat "$LOGDIR"/backend.log "$LOGDIR"/backend.log.1 2>/dev/null \
        | grep -c "$token") || true
    echo "${n:-0}"
}

# (1) Lost audit rows. One occurrence is worth reading: the change it describes
#     already committed, and this line is all that is left of it.
n=$(count_token "AUDIT_WRITE_FAILED")
[ "$n" -gt 0 ] && ALERT+="🔴 AUDIT_WRITE_FAILED x$n — audit rows were lost; the business changes they describe DID commit\n"

# (2) Actions with no identity behind them. Almost always means AUTH_ENABLED was
#     switched off (the kill switch) and nobody switched it back.
n=$(count_token "AUDIT_ANONYMOUS")
[ "$n" -gt 0 ] && ALERT+="🟠 AUDIT_ANONYMOUS x$n — actions recorded with a NULL actor; check AUTH_ENABLED\n"

# (3) Write endpoints nobody wired up. Emitted by AuditMissingMiddleware for a
#     successful non-GET that produced no audit row and is not on the exempt
#     register in core/audit_missing_middleware.py.
n=$(count_token "AUDIT_MISSING")
[ "$n" -gt 0 ] && ALERT+="🟡 AUDIT_MISSING x$n — a write endpoint is not recording anything (see AUDIT_EXEMPT_ROUTES)\n"

# (4) Volume. ⚠ Read the database IN PLACE with sqlite3.
#     users.db is WAL + a thread-local connection pool that never closes, so the
#     autocheckpoint threshold is rarely reached and the main .db file can sit
#     days behind reality. `cp`/`scp` of the single file therefore hands you a
#     stale snapshot AND DOES NOT ERROR. sqlite3 against the live path reads the
#     -wal too, which is why this is the only correct form here.
today=""
if [ -r "$DB" ] && command -v sqlite3 >/dev/null 2>&1; then
    today=$(sqlite3 -readonly "$DB" \
        "SELECT COUNT(*) FROM audit_log WHERE at > strftime('%Y-%m-%dT%H:%M:%SZ','now','-1 day');" 2>/dev/null)
fi
if ! [[ "$today" =~ ^[0-9]+$ ]]; then
    # Unreadable is itself a finding: a health check that silently reports OK
    # when it cannot see the table is worse than no health check.
    ALERT+="🟠 audit_log unreadable at $DB (sqlite3 missing, or no read permission)\n"
    today="?"
elif [ "$today" -gt "$DAILY_ROW_ALERT" ]; then
    ALERT+="🟠 audit_log grew by $today rows in 24h (normal <20) — someone may be hammering a write endpoint\n"
fi

if [ -n "$ALERT" ]; then
    printf '%b' "$ALERT"
    exit 1
fi
echo "✅ audit health OK (24h: $today rows)"
