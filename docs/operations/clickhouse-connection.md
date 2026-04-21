# ClickHouse 生产环境连接指南

本文档介绍如何连接到 ClickHouse 生产环境数据库。

> 变更记录：原东京集群（`dwsz2tfd9y.ap-northeast-1`）及 `Fxbo_Trades` 数据库已下线，
> 全部业务统一使用新加坡 prod 集群与 `KCM_fxbackoffice`（CDC 目标库）。

## 1. 连接配置信息

连接到生产环境需要以下核心参数。请确保这些信息保存在安全的环境变量中，不要直接硬编码在代码里。

| 参数 | 环境变量 Key | 说明 |
| :--- | :--- | :--- |
| **Host** | `CLICKHOUSE_HOST` | `ny8bvfks7d.ap-southeast-1.aws.clickhouse.cloud` |
| **Port** | `CLICKHOUSE_PORT` | `8443` |
| **User** | `CLICKHOUSE_USER` | `default`（或指定的生产用户） |
| **Password** | `CLICKHOUSE_PASSWORD` | （请查阅密钥管理器或 `.env` 文件） |
| **Secure** | - | `True`（必须使用 TLS 加密） |
| **Database** | `CLICKHOUSE_DB` | `KCM_fxbackoffice` |

后端 `clickhouse_service.py` 里额外保留了一组 `CLICKHOUSE_prod_*` 变量，在单集群架构下它们应与上面的值保持一致；保留该命名是为了兼容旧代码中 `get_client(use_prod=True)` 的显式切换。

## 2. Python 连接示例

项目推荐使用 `clickhouse-connect` 库进行连接。

### 安装依赖
```bash
pip install clickhouse-connect
```

### 代码示例
```python
import clickhouse_connect
import os
from dotenv import load_dotenv

load_dotenv()

def get_prod_client():
    """Return a ClickHouse client connected to the prod CDC cluster (KCM_fxbackoffice)."""
    client = clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "ny8bvfks7d.ap-southeast-1.aws.clickhouse.cloud"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8443")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD"),
        secure=True,
        database=os.getenv("CLICKHOUSE_DB", "KCM_fxbackoffice"),
    )
    return client

if __name__ == "__main__":
    try:
        client = get_prod_client()
        result = client.query("SELECT 1").result_set[0][0]
        print(f"ClickHouse 连接成功! 测试查询结果: {result}")
    except Exception as e:
        print(f"连接失败: {e}")
```

## 3. 注意事项
- **安全性**：生产环境强制要求 `secure=True`。
- **超时处理**：ClickHouse Cloud 实例在长时间无活动后可能会进入休眠（Paused）状态，首次连接可能需要 30–60 秒唤醒。
- **数据表说明**：全部业务表统一存储在 `KCM_fxbackoffice` 数据库下，通过 AWS DMS + ClickPipes 从 MySQL CDC 同步。迁移详情见 `clickhosue-pipeline/migration_plan.md`。
