# Git Workflow — KCM IT System

This document describes the recommended git workflow for solo / small-team development on this project. The goal is **safety and traceability**, not ceremony.

## Core principles

1. **Dev and Prod share the same codebase.** What differs is the branch/tag deployed and the env vars (`.env.development` vs `.env.production`, `docker-compose.dev.yml` vs `docker-compose.prod.yml`).
2. **`main` is always deployable.** Prod runs from `main` (or a tag of `main`). Don't push half-finished work to `main`.
3. **Manual deploy is fine.** `./deploy.sh` is run by hand. For an internal financial-risk tool, "look once before deploying" is a feature, not a bug.
4. **DBs stay on the server.** SQLite files (`*.db`) are gitignored. They hold runtime state (risk-monitor config, audit logs, export tasks) and are local to the prod Linux host.

## Branch model

```
main                ← always deployable. prod runs this.
  └── feat-xxx      ← one branch per feature / fix / refactor
  └── fix-xxx
  └── chore-xxx
```

No long-lived `dev` / `staging` branches. Every change goes through a short-lived feature branch.

### Naming

| Prefix | When |
|---|---|
| `feat-` | New feature or page |
| `fix-` | Bug fix |
| `refactor-` | Code restructuring, no behavior change |
| `chore-` | Deps, config, build, CI |
| `docs-` | Documentation only |

Keep branch names short and English (kebab-case): `feat-quick-profit-rule`, `fix-risk-monitor-cen`, `refactor-login-ip-export`.

## Daily workflow

### 1. Start a new piece of work

```bash
git checkout main
git pull origin main                # get latest
git checkout -b feat-add-new-rule
```

### 2. Develop on the Linux host (SSH from Mac VSCode)

- Edit, run `docker compose -f docker-compose.dev.yml up -d` in `backend/` and `frontend/`
- Test in browser at `http://10.6.20.138:5173`
- Commit small logical chunks:
  ```bash
  git add <specific files>
  git commit -m "feat(risk-monitor): add new rule X"
  ```
- (Optional but recommended) use the `smart-commit` skill at `.cursor/skills/smart-commit/SKILL.md` for commit-message conventions.

### 3. Push and open a PR

```bash
git push -u origin feat-add-new-rule
```

Then on GitHub:
1. Open a PR from `feat-add-new-rule` → `main`
2. **Review your own diff** in the GitHub UI — you'll catch things you missed in the editor (debug logs left in, console.logs, commented-out code, etc.)
3. Write a short PR description (what + why; "fixes the CEN currency missing tag on quick-profit alerts")
4. Merge (squash or regular — pick one and stay consistent)

### 4. Deploy to prod

After merging:
```bash
git checkout main
git pull origin main
./deploy.sh
```

Open `http://10.6.20.138:3000` (or `analysis.kohleservices.com`) and smoke-test.

## Tagging releases

For meaningful milestones (new feature shipped, big bugfix, before risky refactor), tag `main`:

```bash
git tag -a v2026.05.12 -m "Risk Monitor 三 Tab 上线"
git push origin v2026.05.12
```

Use a date-based tag like `v2026.05.12` (or `v2026.05.12-1` for second tag same day). Tags give you a named rollback target.

### Rollback procedure (if a deploy goes bad)

```bash
git checkout v2026.05.11    # previous good tag
./deploy.sh                  # redeploy that version
# then on Mac/another shell:
git checkout main
git revert <bad-commit-sha>  # create a revert commit
git push origin main
```

Never `git reset --hard` on `main` after it's been pushed — it rewrites history that others (and prod) may have pulled.

## What NOT to do

| ❌ Don't | ✅ Do instead |
|---|---|
| Commit directly to `main` | Branch → PR → merge |
| `git push --force` to `main` | `git revert <sha>` to undo |
| Commit `.env` files | They're gitignored — keep it that way |
| Commit `*.db` files | They're gitignored — they live on the prod host |
| Commit `backend/logs/*.log` | Gitignored. Logs are runtime artifacts |
| Commit `node_modules`, `__pycache__`, `*.venv` | All gitignored |
| Leave stale feature branches around | Delete after merge: `git branch -d feat-xxx` |
| Skip `git pull` before starting new work | Always `git pull origin main` first |

## Handling deprecated / hidden code

When a page or feature is replaced (e.g. `ClientPnLMonitor.tsx` → `ClientPnLAnalysis.tsx`), don't keep the old file forever:

1. First pass: remove from sidebar, mark as `[HIDDEN]` in `PROJECT_CONTEXT.md`
2. After 1-2 months with no rollback needed: delete the file in a `chore-remove-deprecated-X` branch
3. Update `docs/ai-context/PROJECT_CONTEXT.md` to reflect the deletion

This keeps the working tree clean and avoids accidentally editing dead code.

## Local cleanup

### List local branches

```bash
git branch
```

### Delete merged feature branches

```bash
git branch --merged main | grep -v '^\* main' | xargs -r git branch -d
```

### Prune remote-tracking branches that no longer exist on GitHub

```bash
git fetch --prune
```

### Check for stale git worktrees (e.g. left over from Cursor parallel-edit)

```bash
git worktree list
git worktree remove <path>      # safe — only removes clean ones
git worktree remove --force <path>   # if it has uncommitted changes (verify first!)
```

## Reference: what's in `.gitignore` today

```
.env (all variants)            # secrets
*.db                            # SQLite runtime state
*.log                           # runtime logs
*.json, *.svg, *.txt, *.csv, *.xlsx  # generated artifacts
node_modules/, __pycache__/, *venv  # deps
.cursor/, .vscode/, .agents/    # editor / AI tooling
```

If you need to commit a specific JSON/CSV (e.g. a fixture), force-add it:
```bash
git add -f path/to/fixture.json
```

## Quick reference

```bash
# New work
git checkout main && git pull
git checkout -b feat-xxx

# Save & push
git add <files> && git commit -m "..."
git push -u origin feat-xxx

# After PR merged
git checkout main && git pull
git branch -d feat-xxx
./deploy.sh

# Tag a release
git tag -a v2026.05.12 -m "..."
git push origin v2026.05.12
```
