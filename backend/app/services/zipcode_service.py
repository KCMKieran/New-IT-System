from __future__ import annotations

from typing import List, Dict, Any
import os
from datetime import datetime, timedelta, timezone
import psycopg2
from psycopg2.extras import RealDictCursor


def _pg_dsn() -> str:
	"""Build PostgreSQL DSN from env (MT5_ETL)."""
	host = os.getenv("POSTGRES_HOST", "localhost")
	port = int(os.getenv("POSTGRES_PORT", "5432"))
	db = os.getenv("POSTGRES_DBNAME_MT5", "MT5_ETL")
	user = os.getenv("POSTGRES_USER", "postgres")
	password = os.getenv("POSTGRES_PASSWORD", "")
	return f"host={host} port={port} dbname={db} user={user} password={password}"


def get_zipcode_distribution() -> List[Dict[str, Any]]:
	"""
	Return zipcode distribution from pnl_client_summary (enabled clients only).
	For categories with client_count < 10, also return client_ids array to support UI detail display.
	"""
	sql = r"""
	WITH base AS (
	  SELECT
	    COALESCE(NULLIF(TRIM(zipcode), ''), 'UNKNOWN') AS zipcode,
	    client_id
	  FROM public.pnl_client_summary
	  WHERE is_enabled = 1
	),
	agg AS (
	  SELECT
	    zipcode,
	    COUNT(*)::bigint AS client_count,
	    CASE WHEN COUNT(*) < 10
	         THEN ARRAY_AGG(client_id ORDER BY client_id)
	         ELSE NULL
	    END AS client_ids
	  FROM base
	  GROUP BY zipcode
	)
	SELECT zipcode, client_count, client_ids
	FROM agg
	ORDER BY client_count DESC, zipcode ASC;
	"""
	dsn = _pg_dsn()
	with psycopg2.connect(dsn) as conn:
		with conn.cursor(cursor_factory=RealDictCursor) as cur:
			cur.execute(sql)
			rows = cur.fetchall()
			# Normalize to plain dicts for FastAPI JSON
			return [dict(r) for r in rows]


def get_zipcode_changes(
	start: str | None = None,
	end: str | None = None,
	client_id: int | None = None,
	page: int = 1,
	page_size: int = 50,
) -> Dict[str, Any]:
	"""
	Paginated zipcode change logs within a time window (default: last 25 hours).
	Optional client_id filter to search for specific client's changes.
	If client_id is provided without time range, search all time (no time restriction).
	Returns { rows, total, page, page_size, data: [...] }
	"""
	# Build WHERE conditions dynamically
	where_parts = []
	params = []
	
	# Time range conditions: only apply default 25h window if client_id is NOT provided
	# If client_id is provided without time range, don't restrict by time
	if start is not None or end is not None:
		# User provided explicit time range
		now_utc = datetime.now(timezone.utc)
		if end is None:
			end_dt = now_utc
		else:
			end_dt = None
		if start is None:
			start_dt = now_utc - timedelta(hours=25)
		else:
			start_dt = None
		
		if start_dt is not None and end_dt is not None:
			where_parts.append("change_time BETWEEN %s AND %s")
			params.extend([start_dt, end_dt])
		elif start_dt is None and end_dt is None:
			where_parts.append("change_time BETWEEN %s::timestamptz AND %s::timestamptz")
			params.extend([start, end])
		elif start_dt is None:
			where_parts.append("change_time BETWEEN %s::timestamptz AND %s")
			params.extend([start, end_dt])
		else:
			where_parts.append("change_time BETWEEN %s AND %s::timestamptz")
			params.extend([start_dt, end])
	elif client_id is None:
		# No client_id and no time range: use default 25h window
		now_utc = datetime.now(timezone.utc)
		start_dt = now_utc - timedelta(hours=25)
		end_dt = now_utc
		where_parts.append("change_time BETWEEN %s AND %s")
		params.extend([start_dt, end_dt])
	# else: client_id provided but no time range - don't add time filter
	
	# Client ID filter
	if client_id is not None:
		where_parts.append("client_id = %s")
		params.append(client_id)
	
	where_clause = " AND ".join(where_parts)

	dsn = _pg_dsn()
	with psycopg2.connect(dsn) as conn:
		with conn.cursor() as cur:
			# Count total
			cur.execute(
				f"""
				SELECT COUNT(*) 
				FROM public.swapfree_zipcode_changes
				WHERE {where_clause}
				""",
				params,
			)
			total = int(cur.fetchone()[0])

		with conn.cursor(cursor_factory=RealDictCursor) as cur:
			offset = max(0, (page - 1) * page_size)
			# Fetch data with pagination
			cur.execute(
				f"""
				SELECT client_id, zipcode_before, zipcode_after, change_reason, change_time
				FROM public.swapfree_zipcode_changes
				WHERE {where_clause}
				ORDER BY change_time DESC
				LIMIT %s OFFSET %s
				""",
				params + [page_size, offset],
			)
			data = [dict(r) for r in cur.fetchall()]
			return {"rows": total, "page": page, "page_size": page_size, "data": data}


def get_exclusions(is_active: bool | None = None, limit: int = 100) -> List[Dict[str, Any]]:
	"""
	List exclusions; optional is_active filter.
	Returns [{ id, client_id, reason_code, note, added_by, added_at, expires_at, is_active }]
	"""
	dsn = _pg_dsn()
	with psycopg2.connect(dsn) as conn:
		with conn.cursor(cursor_factory=RealDictCursor) as cur:
			if is_active is None:
				cur.execute(
					"""
					SELECT id, client_id, reason_code, note, added_by, added_at, expires_at, is_active
					FROM public.swapfree_exclusions
					ORDER BY is_active DESC, added_at DESC, id DESC
					LIMIT %s
					""",
					(limit,)
				)
				rows = cur.fetchall()
			else:
				cur.execute(
					"""
					SELECT id, client_id, reason_code, note, added_by, added_at, expires_at, is_active
					FROM public.swapfree_exclusions
					WHERE is_active = %s
					ORDER BY added_at DESC, id DESC
					LIMIT %s
					""",
					(is_active, limit)
				)
				rows = cur.fetchall()
			return [dict(r) for r in rows]


def add_manual_exclusion(client_id: int, note: str, added_by: str = "WebUser") -> Dict[str, Any]:
	"""
	Insert a manual exclusion with reason_code MANUAL, capturing UI note for audit.
	"""
	dsn = _pg_dsn()
	with psycopg2.connect(dsn) as conn:
		with conn.cursor(cursor_factory=RealDictCursor) as cur:
			# Step 1: deactivate active PERM_LOSS record if exists
			cur.execute(
				"""
				UPDATE public.swapfree_exclusions
				SET is_active = FALSE,
					expires_at = NOW()
				WHERE client_id = %s
				  AND reason_code = 'PERM_LOSS'
				  AND is_active = TRUE
				""",
				(client_id,),
			)
			# Step 2: upsert MANUAL record (activate + refresh note)
			cur.execute(
				"""
				INSERT INTO public.swapfree_exclusions (client_id, reason_code, note, added_by, is_active)
				VALUES (%s, 'MANUAL', %s, %s, TRUE)
				ON CONFLICT (client_id) WHERE is_active
				DO UPDATE
				SET reason_code = 'MANUAL',
					note = EXCLUDED.note,
					added_by = EXCLUDED.added_by,
					added_at = NOW(),
					expires_at = NULL,
					is_active = TRUE
				RETURNING id, client_id, reason_code, note, added_by, added_at, expires_at, is_active
				""",
				(client_id, note, added_by),
			)
			row = cur.fetchone()
			return dict(row)


def get_change_frequency(window_days: int = 30, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
	"""
	Count zipcode change events per client in the given window (default 30 days).
	Returns { rows, page, page_size, window_days, data: [{ client_id, changes, last_change }] }
	"""
	window_days = max(1, min(window_days, 365))
	dsn = _pg_dsn()
	with psycopg2.connect(dsn) as conn:
		with conn.cursor() as cur:
			cur.execute(
				"""
				WITH base AS (
				  SELECT client_id, change_time
				  FROM public.swapfree_zipcode_changes
				  WHERE change_time >= NOW() - (%s || ' days')::interval
				),
				agg AS (
				  SELECT client_id, COUNT(*)::bigint AS changes, MAX(change_time) AS last_change
				  FROM base
				  GROUP BY client_id
				)
				SELECT COUNT(*) FROM agg
				""",
				(window_days,),
			)
			total = int(cur.fetchone()[0])
		with conn.cursor(cursor_factory=RealDictCursor) as cur:
			offset = max(0, (page - 1) * page_size)
			cur.execute(
				"""
				WITH base AS (
				  SELECT client_id, change_time
				  FROM public.swapfree_zipcode_changes
				  WHERE change_time >= NOW() - (%s || ' days')::interval
				),
				agg AS (
				  SELECT client_id, COUNT(*)::bigint AS changes, MAX(change_time) AS last_change
				  FROM base
				  GROUP BY client_id
				)
				SELECT client_id, changes, last_change
				FROM agg
				ORDER BY changes DESC, last_change DESC
				LIMIT %s OFFSET %s
				""",
				(window_days, page_size, offset),
			)
			data = [dict(r) for r in cur.fetchall()]
			return {"rows": total, "page": page, "page_size": page_size, "window_days": window_days, "data": data}


