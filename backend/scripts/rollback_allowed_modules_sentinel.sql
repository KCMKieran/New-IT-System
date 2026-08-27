-- ============================================================================
-- ROLLBACK for migrate_allowed_modules_sentinel.sql   (auth, 2026-08-27)
--
-- Forward migration: backend/scripts/migrate_allowed_modules_sentinel.sql
-- ============================================================================
--
-- WHEN YOU NEED THIS
-- ------------------
-- You are rolling the API image back to a build from before 2026-08-27, AND the
-- forward migration has already run.
--
-- That combination is the whole reason this file exists. Rolling the image back
-- does NOT roll the data back, and the old image does not know what "*" means —
-- it reads '["*"]' as a list containing one unrecognised module key, i.e. a
-- grant of NOTHING. Everyone the migration touched loses every page. Managers
-- keep working (they pass the module gate on role alone), so the report you get
-- is "some people lost the whole app and some did not", which is the slowest
-- possible shape of an incident to read.
--
-- ⚠ Run this in the SAME operation as the image rollback, not after somebody
-- complains. The forward direction is safe to leave for later; this one is not.
--
--
-- WHAT IT DOES
-- ------------
-- Puts back the SQL NULL that used to mean "every module, including ones added
-- later". Only rows holding exactly the sentinel are touched — explicit key
-- lists ('["cs","data"]', '[]') are values every old build already understands,
-- so they are left alone.
--
-- Idempotent: a second run matches nothing.
--
-- ⚠ It cannot undo the /cfg/managers narrowing of vincent.shih / tobe.wong /
-- yuna.wong, and should not try: they hold explicit five-key lists, which the
-- old code reads correctly. Restoring their "everything" grant is a permission
-- decision, not a rollback step — do it on the admin page if it is wanted, so
-- that it leaves an audit row like any other grant change.
--
--
-- HOW TO RUN IT
-- -------------
-- Same constraints as the forward migration: in place, never on a copy
-- (backend/data/users.db is WAL + a never-closed connection pool, so a copied
-- .db file is a silently stale snapshot). Container route first — python's
-- driver has a 5s busy timeout, the sqlite3 CLI has none:
--
--   docker cp backend/scripts/rollback_allowed_modules_sentinel.sql \
--             new-it-backend-prod:/tmp/rollback.sql
--   docker exec new-it-backend-prod python -c "
--   import sqlite3; c=sqlite3.connect('/app/data/users.db')
--   c.executescript(open('/tmp/rollback.sql').read()); c.commit(); c.close()"
--
--   # verify — expect ZERO sentinel rows left
--   docker exec new-it-backend-prod python -c "
--   import sqlite3; c=sqlite3.connect('/app/data/users.db')
--   print('remaining sentinels (want 0):',
--         c.execute(\"SELECT COUNT(*) FROM users WHERE allowed_modules = '[\\\"*\\\"]'\").fetchone()[0])
--   for r in c.execute('SELECT email,role,allowed_modules FROM users ORDER BY id'):
--       print(r)"
--
-- On the host (needs root):
--
--   sudo sqlite3 /opt/myproject/New-IT-System/backend/data/users.db \
--        < backend/scripts/rollback_allowed_modules_sentinel.sql
--
-- No restart needed: allowed_modules is re-read per request.
-- ============================================================================

PRAGMA busy_timeout = 5000;

BEGIN;

UPDATE users
   SET allowed_modules = NULL
 WHERE allowed_modules = '["*"]';

-- Expected: the same count step 1 of the forward migration reported, plus any
-- account granted "everything" on /cfg/managers since. A 0 here means either
-- the forward migration never ran or this rollback already did.
SELECT changes() AS rows_reverted;

COMMIT;
