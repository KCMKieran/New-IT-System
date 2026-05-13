# Prod 外网"失联请重试"故障修复

> **日期**: 2026-05-13
> **影响范围**: 外网 `analysis.kohleservices.com` 用户频繁看到 `LazyErrorBoundary` 全屏"页面加载失败"页
> **状态**: P0/P1 共 10 项已上线;CF Dashboard Cache Rule 待人工配置

---

## 根因

| # | 根因 | 证据 |
|---|---|---|
| 1 | `index.html` 无 `Cache-Control`,CF 边缘 + 浏览器缓存旧 HTML → `./deploy.sh` 后旧 chunk 404 → ChunkLoadError | `frontend/nginx.conf` 原仅给 `/assets` 设强缓存,`/` 无任何 header |
| 2 | 后端 `uvicorn --workers 1` + APScheduler 同进程 → 单点阻塞放大失败 | `backend/Dockerfile` 原 `--workers 1` |
| 3 | `lazyWithRetry` 仅重试 2 次/1s 间隔,移动网络抖动 >2s 必失败 | `LazyErrorBoundary.tsx` 默认参数 |
| 4 | `apiFetch` 无超时、无重试、无错误上报 — 失败发生时无可观测性 | `frontend/src/lib/fetch.ts` 原仅注入 X-API-Key |
| 5 | 部分 dashboard widget 静默吃错(`setRows([])` 不报错) | `ReturnRateSummary.tsx`、`Profit.tsx` |

---

## 改动总结

| # | 类别 | 文件 | 改动 | 预期效果 |
|---|---|---|---|---|
| P0-1 | nginx | `frontend/nginx.conf` | `index.html` 加 `Cache-Control: no-cache, no-store, must-revalidate` | 浏览器 / CF 不再持有旧 HTML |
| P0-2 | 前端 | `LazyErrorBoundary.tsx` | 自动 reload 时附加 `?_cb=<ts>` cache buster | reload 100% 拿到新 HTML |
| P0-3 | 前端 | `LazyErrorBoundary.tsx` | 重试 2→3 次 + 1.5s 指数退避,仅对 chunk 错误重试 | 总重试窗口 ~7s,QUIC/移动友好 |
| P0-4 | CF | Cloudflare Dashboard(**待人工**) | Cache Rule: hostname=`analysis.kohleservices.com` AND path=`/` 或 `.html` → Bypass cache | CF 边缘不再缓存旧 HTML |
| P0-5 | 验证 | `docker logs new-it-frontend-prod` | grep `/assets/*.js.*" 404` 抓基线 | 数据驱动后续观测 |
| P1-6 | 前端 | `frontend/src/lib/fetch.ts`(重写) | 60s 超时 + 1 次自动重试(5xx/网络错)+ 与调用者 signal 合并 | 偶发抖动用户无感;21 处调用零改动 |
| P1-7 | 前/后端 | `LazyErrorBoundary.tsx` + 新建 `backend/app/api/v1/routes/client_log.py` + `routers.py` | 全局错误 → `POST /api/v1/log/client-error`(keepalive + X-API-Key)→ `backend.log` 带 trace_id | 主动看见客户端崩溃,不再靠口头反馈 |
| P1-8 | 后端 | `backend/Dockerfile` + `backend/app/main.py` | `uvicorn --workers 1 → 4` + `/tmp/.scheduler.lock` flock 单实例守 scheduler | API 并发 ×4;定时任务仍单实例 |
| P1-9 | 前端 | `ReturnRateSummary.tsx`、`Profit.tsx` | catch 块新增 `setError` + 红色文字显示;`AbortError` 跳过 | 失败显式可见,不再"no rows" 误导 |
| P1-10 | nginx | `frontend/nginx.conf` | 限速 30→60r/s,burst 50→120 | NAT 共享 IP + Dashboard 5 并发不再 429 |

---

## 关键技术点

### scheduler 文件锁(P1-8)

**坑**: 一开始把锁放在 `/app/data/.scheduler.lock` — 但 dev 和 prod 容器**共享同一个 bind-mount 卷**,dev worker 抢到锁后 prod 4 workers 全部失败。

**修复**: 改用容器本地 `/tmp/.scheduler.lock` — 每个容器有独立的 `/tmp` 层,不会跨容器干扰。

```python
SCHEDULER_LOCK_PATH = "/tmp/.scheduler.lock"  # 容器本地,不能用 /app/data
# fcntl.flock(LOCK_EX | LOCK_NB) — kernel 在 worker 死亡时自动释放
```

### apiFetch 超时实现(P1-6)

**关键**: 与调用者的 `AbortController.signal` **合并**,不能覆盖。21 个文件已有的 useEffect AbortController 必须继续工作。

```ts
const controller = new AbortController()
if (externalSignal) externalSignal.addEventListener("abort", () => controller.abort(...))
const timer = setTimeout(() => controller.abort(new DOMException(..., "TimeoutError")), timeoutMs)
return fetch(input, { ...init, signal: controller.signal })
```

### keepalive 而非 sendBeacon(P1-7)

`navigator.sendBeacon` 不能带自定义 header → 必须给 endpoint 开 X-API-Key 白名单(安全降级)。

改用 `fetch(..., { keepalive: true })`:页面卸载时仍能完成发送,且可带 `X-API-Key`。

---

## 验证结果

| 验证项 | 结果 |
|---|---|
| `curl -I http://10.6.20.138:3000/` 返回 `Cache-Control: no-cache, no-store, must-revalidate` | ✅ |
| `curl -I .../assets/*.js` 仍 `max-age=31536000` | ✅ |
| backend.log 见 `Worker pid=10 owns scheduler lock — starting schedulers` + 3 个 `HTTP only` | ✅ |
| `burst-open scan complete` 日志正常 | ✅ |
| `POST /api/v1/log/client-error` 无 key → 403;有 key → 204 + backend.log Warning | ✅ |
| nginx `rate=60r/s burst=120` 已生效 | ✅ |

---

## ⚠️ 还需手动完成

**Cloudflare Dashboard 加 Cache Rule** —— 不做此步,P0-1 在 CF 边缘那层无效。

路径: Cloudflare → Caching → **Cache Rules** → Create rule
- Name: `Bypass HTML for analysis`
- When: `(http.host eq "analysis.kohleservices.com" and (http.request.uri.path eq "/" or ends_with(http.request.uri.path, ".html")))`
- Then: Cache eligibility = **Bypass cache**

---

## 后续观测命令

```bash
# 1. chunk 404 数量(应趋零)
docker logs new-it-frontend-prod 2>&1 | grep -c '/assets/.*\.js.*" 404'

# 2. 客户端上报的错误趋势
docker exec new-it-backend-prod grep "Client error:" /app/logs/backend.log | tail -50

# 3. scheduler 单实例验证(每次重启应只有 1 个 "owns scheduler lock")
docker exec new-it-backend-prod grep "owns scheduler\|HTTP only" /app/logs/backend.log

# 4. /tmp 锁文件状态
docker exec new-it-backend-prod ls -la /tmp/.scheduler.lock
```

一周内 client-error 日志中 `kind=chunk_load` 接近 0 → 修复成功。

---

## 未在本次范围(中长期路线图)

- 拆 scheduler 独立容器(根治 worker 卡死锁不释放)
- 同步 DB 调用走 `run_in_threadpool` 审计
- vendor-three.js 动态 import(GoldQuote 才用)
- Service Worker 离线缓存
- CF Tunnel 改 HTTP/2(降 QUIC 抖动)
- Sentry / 自建错误聚合面板

详见 `/home/kcm-trade/.claude/plans/table-scalable-forest.md` 中"不在本计划范围"一节。
