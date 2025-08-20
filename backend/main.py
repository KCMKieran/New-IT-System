# main.py
from fastapi import FastAPI
from pydantic import BaseModel
import os
from dotenv import load_dotenv
import pandas as pd
import pymysql
import duckdb

# fresh grad: load .env for DB credentials
load_dotenv()

# Create an instance of the FastAPI class
# 这是一个核心对象，你的所有API都将通过它来注册
app = FastAPI()

# Define a path operation decorator
# @app.get("/") 告诉FastAPI，任何对根路径 ("/") 的GET请求都应由下面的函数处理
@app.get("/")
def read_root():
    """
    Root endpoint, returns a welcome message.
    This is useful for health checks.
    """
    return {"message": "Welcome to the Compliance Monitoring System API"}


class AggregateRequest(BaseModel):
    symbol: str = "XAUUSD"
    start: str  # e.g. "2025-05-01 00:00:00"
    end: str    # e.g. "2025-08-31 23:59:59"


@app.post("/aggregate-to-json")
def aggregate_to_json(req: AggregateRequest):
    """
    1) Reads MySQL with filters (symbol, time range)
    2) Writes raw result to Parquet (backend/data/orders.parquet)
    3) Uses DuckDB to aggregate (per day x hour) and writes JSON to frontend/public

    Notes for fresh grads:
    - We export ONLY aggregated JSON for the frontend
    - .env keys expected:
      MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB
    """

    DB_CONFIG = {
        'host': os.environ.get('DB_HOST'),
        'user': os.environ.get('DB_USER'),
        'password': os.environ.get('DB_PASSWORD'),
        'database': os.environ.get('DB_NAME'),
        'port': int(os.environ.get('DB_PORT', '3306')),
        'charset': os.environ.get('DB_CHARSET', 'utf8mb4'),
    }

    # if not all([mysql_host, mysql_user, mysql_password, mysql_db]):
    #     return {"ok": False, "error": ".env missing MYSQL_* variables"}

    # 注意：避免在 SQL 中直接写含 % 的 LIKE 常量，统一改为占位符传参，防止 DBAPI 误把 % 当格式符
    sql = (
        "SELECT ticket, login, symbol, cmd, volume, OPEN_TIME, OPEN_PRICE, CLOSE_TIME, CLOSE_PRICE, swaps, profit "
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

    conn = pymysql.connect(**DB_CONFIG)
    try:
        df = pd.read_sql(
            sql, conn,
            params=[
                req.symbol,
                req.start,
                req.end,
                "%test%",  # `GROUP` LIKE %s
                "%test%",  # name LIKE %s
                "KCM%",     # `GROUP` LIKE %s
                "testKCM%", # `GROUP` LIKE %s
            ],
        )
    finally:
        conn.close()

    os.makedirs("backend/data", exist_ok=True)
    parquet_path = "backend/data/orders.parquet"
    df.to_parquet(parquet_path, engine="pyarrow", index=False)

    os.makedirs("frontend/public", exist_ok=True)
    json_path = "frontend/public/profit_xauusd_hourly.json"

    # DuckDB aggregate to JSON: per day x hour
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
          SELECT
            CAST(OPEN_TIME AS DATE)              AS date,
            EXTRACT(HOUR FROM OPEN_TIME)         AS hour,
            SUM(profit)                          AS profit
          FROM read_parquet('{parquet_path}')
          GROUP BY 1,2
          ORDER BY 1,2
        ) TO '{json_path}' (FORMAT JSON);
        """
    )
    con.close()

    return {"ok": True, "json": json_path, "rows": int(df.shape[0])}