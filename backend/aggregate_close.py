def aggregate_close_time_hourly_to_json(symbol: str = "XAUUSD") -> dict:
    """
    Aggregate hourly profit by CLOSE_TIME and export JSON.
    - Time window: hardcoded to 2025-05-01 00:00:00 ~ 2025-08-19 23:59:59
    - SQL uses ONLY CLOSE_TIME BETWEEN %s AND %s  (no 1970 filter)
    - Output JSON path resolves to <repo_root>/frontend/public/profit_xauusd_hourly_close.json
    """
    import os
    import pandas as pd
    import pymysql
    import duckdb
    from dotenv import load_dotenv

    # Ensure .env is loaded for DB credentials
    load_dotenv()

    # Hardcoded time window (inclusive)
    start = "2025-05-01 00:00:00"
    end = "2025-08-19 23:59:59"

    # DB config from environment variables
    DB_CONFIG = {
        "host": os.environ.get("DB_HOST"),
        "user": os.environ.get("DB_USER"),
        "password": os.environ.get("DB_PASSWORD"),
        "database": os.environ.get("DB_NAME"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "charset": os.environ.get("DB_CHARSET", "utf8mb4"),
    }

    # SQL: filter by CLOSE_TIME only; keep other conditions unchanged
    sql = (
        "SELECT ticket, login, symbol, cmd, volume, OPEN_TIME, OPEN_PRICE, CLOSE_TIME, CLOSE_PRICE, swaps, profit "
        "FROM mt4_live.mt4_trades "
        "WHERE symbol = %s "
        "  AND CLOSE_TIME BETWEEN %s AND %s "
        "  AND login NOT IN ("
        "    SELECT LOGIN FROM mt4_live.mt4_users "
        "    WHERE ((`GROUP` LIKE %s) OR (name LIKE %s)) "
        "      AND ((`GROUP` LIKE %s) OR (`GROUP` LIKE %s))"
        ")"
    )

    # Query to DataFrame
    conn = pymysql.connect(**DB_CONFIG)
    try:
        df = pd.read_sql(
            sql,
            conn,
            params=[
                symbol,
                start,
                end,
                "%test%",   # GROUP LIKE
                "%test%",   # name LIKE
                "KCM%",     # GROUP LIKE
                "testKCM%", # GROUP LIKE
            ],
        )
    finally:
        conn.close()

    # Resolve repo root: <repo_root>/backend/<this file> -> go up one level
    backend_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(backend_dir)

    # Save raw to parquet under backend/data
    data_dir = os.path.join(repo_root, "backend", "data")
    os.makedirs(data_dir, exist_ok=True)
    parquet_path = os.path.join(data_dir, "orders_close.parquet")
    df.to_parquet(parquet_path, engine="pyarrow", index=False)

    # Aggregate by CLOSE_TIME hour -> JSON under frontend/public (sibling of backend)
    public_dir = os.path.join(repo_root, "frontend", "public")
    os.makedirs(public_dir, exist_ok=True)
    json_close_path = os.path.join(public_dir, "profit_xauusd_hourly_close.json")

    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
          SELECT
            CAST(CLOSE_TIME AS DATE)       AS date,
            EXTRACT(HOUR FROM CLOSE_TIME)  AS hour,
            SUM(profit)                    AS profit
          FROM read_parquet('{parquet_path}')
          GROUP BY 1,2
          ORDER BY 1,2
        ) TO '{json_close_path}' (FORMAT JSON);
        """
    )
    con.close()

    return {"ok": True, "json_close": json_close_path, "rows": int(df.shape[0])}


if __name__ == "__main__":
    # 直接运行此文件时的测试参数
    symbol = "XAUUSD"
    
    result = aggregate_close_time_hourly_to_json(symbol)
    print(f"聚合完成: {result}")
