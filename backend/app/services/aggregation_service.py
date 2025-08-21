from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd
import pymysql

from ..core.config import Settings


def aggregate_to_json(settings: Settings, symbol: str, start: str, end: str) -> dict:
	"""
	Reuses existing logic: read MySQL -> write parquet -> DuckDB aggregate -> write JSON.
	Paths and DB config come from Settings to avoid hardcoding.
	"""

	# DB connection
	conn = pymysql.connect(
		host=settings.DB_HOST,
		user=settings.DB_USER,
		password=settings.DB_PASSWORD,
		database=settings.DB_NAME,
		port=int(settings.DB_PORT),
		charset=settings.DB_CHARSET,
	)

	sql = (
		"SELECT ticket, login, symbol, cmd, volume, OPEN_TIME, OPEN_PRICE, "
		"CLOSE_TIME, CLOSE_PRICE, swaps, profit "
		"FROM mt4_live.mt4_trades "
		"WHERE symbol = %s "
		"  AND OPEN_TIME BETWEEN %s AND %s "
		"  AND CLOSE_TIME != '1970-01-01 00:00:00' "
		"  AND login NOT IN ("
		"    SELECT LOGIN FROM mt4_live.mt4_users "
		"    WHERE ((`GROUP` LIKE %s) OR (name LIKE %s)) "
		"      AND ((`GROUP` LIKE %s) OR (`GROUP` LIKE %s))"
		")"
	)

	try:
		df = pd.read_sql(
			sql,
			conn,
			params=[
				symbol,
				start,
				end,
				"%test%",
				"%test%",
				"KCM%",
				"testKCM%",
			],
		)
	except Exception as exc:
		conn.close()
		return {"ok": False, "error": str(exc)}
	finally:
		try:
			conn.close()
		except Exception:
			pass

	# Ensure directories
	parquet_dir: Path = settings.parquet_dir
	parquet_dir.mkdir(parents=True, exist_ok=True)
	parquet_path = parquet_dir / "orders.parquet"

	df.to_parquet(str(parquet_path), engine="pyarrow", index=False)

	public_dir: Path = settings.public_export_dir
	public_dir.mkdir(parents=True, exist_ok=True)
	json_path = public_dir / "profit_xauusd_hourly.json"

	con = duckdb.connect()
	con.execute(
		f"""
		COPY (
		  SELECT
		    CAST(OPEN_TIME AS DATE)              AS date,
		    EXTRACT(HOUR FROM OPEN_TIME)         AS hour,
		    SUM(profit)                          AS profit
		  FROM read_parquet('{str(parquet_path)}')
		  GROUP BY 1,2
		  ORDER BY 1,2
		) TO '{str(json_path)}' (FORMAT JSON);
		"""
	)
	con.close()

	return {"ok": True, "json": str(json_path), "rows": int(df.shape[0])}


