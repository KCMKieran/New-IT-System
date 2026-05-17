# HANDOFF — feat/risk-monitor-realtime

**Branch**: `feat/risk-monitor-realtime`
**Base**: `main` @ `f9cf656`
**Built**: 2026-05-16 Sat night HKT (weekend autonomous batch)
**Status**: code complete, tests green, dev container clean, **not merged to main**, **not pushed to origin**

---

## TL;DR

Three optimizations done in one branch:

| OPT | What | Env flag (default OFF) | Risk if accidentally on |
|---|---|---|---|
| **0011** | Per-server cursor HWM replaces overlap-window SQL polling | `CURSOR_SCAN_ENABLED=true` | Low — cold-start path is the legacy SQL; if cursor logic bugs, retry-on-next-tick covers it |
| **0012** | Scheduler split into 60s fast tier (burst only) + slow tier (QOC+QP) | `BURST_FAST_TIER_ENABLED=true` | Medium — needs OPT-0011 enabled to avoid MySQL load explosion at 60s polling |
| **0013** | SSE push from backend → frontend for real-time UX | `SSE_ENABLED=true` | Low — additive endpoint; if SSE fails frontend silently falls back to existing 10-min polling |

Everything is **additive + env-flag gated**. Doing `git checkout main && git branch -D feat/risk-monitor-realtime` reverses all of it.

---

## Commits on this branch (since main)

```
b687cfc  feat(risk-monitor): OPT-0013 SSE alert push to frontend (env-gated)
9fb5cca  feat(risk-monitor): OPT-0012 scheduler fast/slow tier split (env-gated)
4ee87ae  feat(risk-monitor): OPT-0011 cursor-mode scanning (env-gated, default off)
c59e2f8  chore(opt): claim OPT-0011/0012/0013 for feat/risk-monitor-realtime
```

All 4 commits include a `Co-Authored-By: Claude Opus 4.7 (1M context)` line.

---

## Test results

```
84 passed, 1 warning in 2.51s
```

Breakdown:
- 38 pre-existing (Quick Profit API + service, burst scheduler prev_alerts, floating refresh)
- 6 OPT-0014 (SQLite WAL, from prior session)
- 17 OPT-0011 (scan_cursors + HWM compute + env flag)
- 14 OPT-0012 (scheduler tier dispatch + env flag + lock isolation)
- 9 OPT-0013 (alerts pubsub fan-out + cross-thread + bounded queue)

Plus frontend `npm run build` clean (no new warnings; existing chunk-size notice unrelated).

---

## What you should manually verify when back

### Mandatory (5 min)

1. **Switch to branch + restart dev backend**:
   ```bash
   git checkout feat/risk-monitor-realtime
   cd backend && docker compose -f docker-compose.dev.yml restart && cd ..
   ```

2. **Verify nothing exploded**:
   ```bash
   docker logs new-it-backend-dev --tail 30 | grep -iE "ERROR|exception"
   # Should be empty
   ```

3. **Open `http://10.6.20.138:5173/risk-monitor`** — page should look identical to before (no flags on yet, so behavior unchanged). Check:
   - All 4 tabs render
   - The new small grey dot + "离线" label appears at the right end of the tab list (because `SSE_ENABLED` is off on the dev backend → indicator says "SSE 在后端未启用")

### Optional — turn flags on to see real-time UX (10 min)

Add to `backend/docker-compose.dev.yml` environment block:
```yaml
- SSE_ENABLED=true
- CURSOR_SCAN_ENABLED=true
# Don't enable fast tier in dev — dev compose has BURST_SCAN_ENABLED=false
# so no scheduler runs to publish events anyway.
```

Then restart. Open the page:
- Indicator should turn **green pulse** ("实时") within ~2 sec
- "X s ago" timer should tick up
- No new alerts will arrive because dev backend has no scheduler — to actually see push, you'd need to either (a) enable BURST_SCAN_ENABLED on dev (warning: that doubles writes to the shared SQLite if prod scheduler also runs), or (b) test in prod after merge.

### Prod rollout (when ready — separate decision)

Recommended sequence:
1. **First**: just SSE flag → `SSE_ENABLED=true` in prod compose. Zero scan behavior change, only frontend UX improvement. Easy rollback (set false + restart).
2. **Second**: cursor flag → `CURSOR_SCAN_ENABLED=true`. Watch MySQL query rows scanned for a day; should drop ~10×. Cursors persist in SQLite — no harm if you flip back to false.
3. **Third**: fast tier → `BURST_FAST_TIER_ENABLED=true`. Only after #2 stable for ≥1 day, because fast tier @ 60s WITHOUT cursors means 10× the MySQL load.

---

## Files changed (vs main)

```
backend/app/api/v1/routes/risk_monitor.py        # +75  (SSE endpoint + imports)
backend/app/core/alerts_pubsub.py                # +109 (new — pub/sub module)
backend/app/core/burst_open_scheduler.py         # ~120 (tier dispatch + fast job + SSE publish hook)
backend/app/core/risk_monitor_db.py              # +75  (scan_cursors table + 3 helpers)
backend/app/services/risk_monitor_service.py     # +95  (MT4/MT5 cursor SQL + HWM)
backend/app/services/rule_quick_open_close_service.py  # +85 (cursor SQL + HWM)
backend/tests/test_alerts_pubsub.py              # +210 (new — 9 tests)
backend/tests/test_scan_cursors.py               # +220 (new — 17 tests)
backend/tests/test_scheduler_tiers.py            # +280 (new — 14 tests)
frontend/src/hooks/useRiskMonitorStream.ts       # +130 (new — SSE hook)
frontend/src/pages/RiskMonitor.tsx               # ~30  (indicator wiring)
docs/optimization/backlog.md                     # WIP rows for 0011/0012/0013
docs/optimization/items/OPT-0011-*.md            # status: wip
docs/optimization/items/OPT-0012-*.md            # status: wip
docs/optimization/items/OPT-0013-*.md            # status: wip
.gitignore                                       # backup glob
```

---

## Known limits / follow-ups

| Item | Notes |
|---|---|
| **Frontend per-tab incremental refresh on SSE event** | Hook + indicator are wired but each tab's `setInterval` still drives data fetch. Adding `useEffect(() => refetch(), [lastEvent?.received_at])` in each tab is a 5-line per-tab change — deferred to follow-up so visual UX can be validated first |
| **Cloudflare Tunnel SSE compatibility** | 15s keepalive comment added to survive idle-cut; not verified through the actual tunnel. If prod page loses connection every 30/60s after enabling, tune keepalive down to 10s or switch to long-poll fallback |
| **Cursor schema migration on shared SQLite** | The `scan_cursors` table is created additively (`IF NOT EXISTS`). Prod will pick it up on next backend restart with no impact (no code reads it unless `CURSOR_SCAN_ENABLED=true`) |
| **Fast tier shares `_scan_lock` with slow tier** | Documented as intentional (avoid splitting `_latest_result` mutation). Worst case: ~1 fast tick skipped per 10 min when slow tier overlaps. If this becomes painful, split locks + add a small mutex around `_latest_result` mutations |
| **Quick Profit not cursorized** | Its 30-min aggregation can't fit a strict-greater-than cursor. Untouched on purpose; Quick Profit slow tier still uses time-window SQL (which is correct for its workload) |

---

## Backup

Pre-work SQLite snapshot:
```
backend/data/risk_monitor.db.bak-20260516-2316-pre-realtime  (99 MB)
```

Restore (only if disaster):
```bash
cp backend/data/risk_monitor.db.bak-20260516-2316-pre-realtime \
   backend/data/risk_monitor.db
```

---

## Open OPT items (status = wip in backlog)

After you decide to merge, close each per the optimization-tracker
workflow (`docs/optimization/README.md §D`):
- Add commit SHAs to each item's `## 结果` section
- Move the row from `backlog.md` WIP table → `done.md`
- Flip frontmatter `status: wip` → `status: done`

Or ask me to do it: "close OPT-0011 / OPT-0012 / OPT-0013".

---

## If anything feels wrong

Quickest reset:
```bash
git checkout main
git branch -D feat/risk-monitor-realtime
cd backend && docker compose -f docker-compose.dev.yml restart
```

Branch survives in `git reflog` for 90 days if you change your mind.
