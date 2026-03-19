# Cloudflare Access 拦截 Dashboard API 刷新请求

> **日期**: 2026-03-19
> **状态**: 已修复（方案 B：iframe token 续签）
> **影响范围**: 所有通过 `analysis.kohleservices.com` 外网访问的用户

## 问题描述

通过 Cloudflare Tunnel 外网访问 Dashboard 页面时：

- **首次加载**正常（约 10 秒内），所有 widget 数据都能显示
- **点击任何 widget 的刷新按钮**或**切换筛选条件**后，数据消失
  - 部分组件显示 `"Load failed"` (iOS) / `"Failed to fetch"` (Chrome)
  - ReturnRateSummary 组件显示 AG-Grid 的 `"no rows to show"`（因为其 catch 块静默设置空数组）
- **后端 prod log 中完全没有记录**这些失败的请求
- **刷新整个页面**后又能正常加载（然后再点刷新又失败）

办公室内网 (`10.6.20.x`) 直连 `10.6.20.138:3000` 或通过 `analysis.kohleservices.com`（IP bypass）访问时，一切正常。

## 请求链路

```
外网用户:
  浏览器 → Cloudflare CDN/Zero Trust → Cloudflare Tunnel (QUIC) → Nginx (:3000) → FastAPI (:8001)

内网用户 (直连):
  浏览器 → Nginx (:3000) → FastAPI (:8001)

内网用户 (域名，IP bypass):
  浏览器 → Cloudflare CDN → Tunnel → Nginx (:3000) → FastAPI (:8001)
  （办公室 IP 命中 CF Access Bypass 规则，跳过认证）
```

## 排查证据

### 1. 服务端全部健康

所有到达服务器的请求均返回 `status=200`：

```
# Nginx 日志：零失败请求
$ docker logs new-it-frontend-prod | grep '/api/' | grep -v '" 200 '
(无输出 — 没有任何非 200 的 API 请求)

# 后端日志：所有请求正常完成
[05:44:35] Request completed: status=200 duration=753.62ms  # pnl-by-group
[05:44:35] Request completed: status=200 duration=751.74ms  # client-return-rate
[05:44:35] Request completed: status=200 duration=316.14ms  # pnl-by-sales-team

# 从服务器直接测试 API
$ curl -s -o /dev/null -w "%{http_code}" http://10.6.20.138:3000/api/v1/dashboard/pnl-by-group
200
```

### 2. 外网刷新请求从未到达服务器

对比内网与外网的请求模式：

| 指标 | 办公室内网 (10.6.20.x) | 外网 Cloudflare (182.239.x) |
|------|----------------------|--------------------------|
| 总 API 请求数 | **56** | **33** |
| 含 widget 刷新点击 | **有**（多次独立的 symbol-summary 查询） | **无**（全部是首次加载的批量请求） |

### 3. 前端代码无拦截机制

排查确认：

- **无 Service Worker**
- **无全局 fetch 拦截器/包装器**（不使用 axios）
- **Auth Provider** 仅检查 localStorage，不影响 fetch 调用
- Widget 组件中的 `fetchData()` 函数在首次加载（useEffect）和按钮点击时**完全相同**

### 4. 跨设备/浏览器复现

| 设备 | 浏览器 | 网络 | 首次加载 | 刷新点击 |
|------|--------|------|---------|---------|
| iPhone | Chrome (CriOS/WebKit) | WiFi 外网 | ✓ | ✗ "Load failed" |
| MacBook | Chrome (Chromium) | 手机热点 | ✓ | ✗ "Failed to fetch" / 空数据 |
| 办公室 PC | 任意浏览器 | 内网直连 | ✓ | ✓ |
| 办公室 PC | 任意浏览器 | 域名 (IP bypass) | ✓ | ✓ |

### 5. Network 抓包：铁证

**成功请求**（首次加载，07:24:22，距 JWT 签发 7 秒）：

```
GET /api/v1/open-positions/symbol-summary?symbol=XAUUSD → 200 OK
cookie: CF_AppSession=04ef4a70...; CF_Authorization=eyJhbGci...
x-trace-id: req-a52a897a   ← 请求到达了后端
```

**失败请求**（点击刷新，07:24:32，距 JWT 签发 17 秒）：

```
GET /api/v1/open-positions/symbol-summary?symbol=XAUUSD → 302 Found
cookie: CF_AppSession=04ef4a70...
                                   ← CF_Authorization 已消失！
location: https://kohle.cloudflareaccess.com/cdn-cgi/access/login/...
                                   ← 被重定向到登录页
→ 浏览器 fetch() 跟随 302 到跨域 URL → CORS 拦截 → 网络错误
```

## 根因分析

### CF_Authorization JWT 只有 10 秒有效期

解码成功请求中的 `CF_Authorization` JWT：

```json
{
  "email": "kieran.xiang@kohleservices.com",
  "iat": 1773905055,
  "exp": 1773905065,
  "type": "app",
  "country": "HK"
}
```

**`exp - iat = 10 秒`**。这是 Cloudflare Access 的内部行为，不受 Session Duration 设置影响。

### Cloudflare Access 的双 Cookie 机制

| Cookie | 有效期 | 控制方式 | 作用 |
|--------|--------|----------|------|
| `CF_AppSession` | **6 小时** | Session Duration 设置 | 会话标识，证明用户已登录 |
| `CF_Authorization` | **~10 秒** | Cloudflare 内部固定 | JWT 令牌，实际的认证凭证 |

### 时间线

```
07:24:15  用户完成 Azure AD 认证
          → CF Access 设置 CF_AppSession (6h) + CF_Authorization (10s)
07:24:22  useEffect fetch → CF_Authorization 有效 → 200 ✓（距过期还剩 3 秒）
07:24:25  CF_Authorization 过期 → 浏览器删除 cookie
07:24:32  点击刷新 fetch → 只有 CF_AppSession → CF Access 302 → CORS ✗
```

### 为什么首次加载成功

页面加载时的 `useEffect` fetch 调用在 JWT 签发后 **几秒内** 完成，此时 `CF_Authorization` 尚未过期（10 秒窗口）。

### 为什么全页刷新能恢复

全页刷新是 **navigation 请求**（`Sec-Fetch-Mode: navigate`）。CF Access 检测到有效的 `CF_AppSession`，会签发一个新的 `CF_Authorization`（又是 10 秒），然后 useEffect 在新的 10 秒窗口内完成 fetch。

### 为什么 JS fetch() 不能自动续签

JS `fetch()` 是 `Sec-Fetch-Mode: cors` 请求。CF Access **不会**为 cors 请求自动续签 `CF_Authorization`，而是直接返回 302 重定向到登录页。这是 CF Access 的设计 — 它是为保护网页（navigation）设计的，不是为保护 SPA API（fetch）设计的。

### 为什么内网通过域名访问没问题

办公室 IP 段在 CF Access Policy 中设置了 **Bypass** 规则。Bypass 的请求完全跳过 CF Access 认证层，不需要 `CF_Authorization` cookie。

## 受影响组件

| 组件 | API 端点 | 错误表现 |
|------|----------|---------|
| `CnPaymentSuccessRate` | `/api/v1/dashboard/cn-payment-success-rate` | 显示错误消息 |
| `PositionSummary` | `/api/v1/open-positions/symbol-summary` | 显示错误消息 |
| `ReturnRateSummary` | `/api/v1/client-return-rate/query` | 静默失败，显示 "no rows to show" |
| `Past24hClientPnlByCountry` | `/api/v1/dashboard/pnl-by-sales-team` | 显示错误消息 |
| `Past24hClientPnlByGroup` | `/api/v1/dashboard/pnl-by-group` | 显示错误消息 |

## 可选方案对比

| 方案 | 原理 | 安全性 | 稳定性 | 代码改动 |
|------|------|--------|--------|----------|
| **A: Bypass `/api/*`** | CF Access 跳过 API 路径认证 | 中（需 CORS + 限速补偿） | 高 | 后端 CORS + Nginx |
| **B: iframe token 续签** ✅ | 隐藏 iframe 定期触发 navigation 请求，续签 CF_Authorization | 高（API 仍受 CF Access 保护） | 中 | 前端 iframe hook |
| **C: Bypass + 后端 API Key** | Bypass + 自定义 header 校验 | 高 | 高 | 后端中间件 + 前端 header |
| **D: 延长 Session Duration** | 延长到 30 天 | 高 | 低（不治本，CF_Authorization 仍 10s） | 无 |
| **E: 前端 fetch 重试** | 捕获错误后重试 | — | 低（CF Access 持续拦截，重试也失败） | 前端 |

## 已实施的修复：方案 B — iframe token 续签

### 原理

```
每 8 秒：
  隐藏 iframe 加载 /cf-refresh.html（同域轻量页面）
  → 浏览器发出 navigation 请求（Sec-Fetch-Mode: navigate）
  → CF Access 检测到有效的 CF_AppSession（6 小时）
  → 签发新的 CF_Authorization cookie（10 秒）
  → cookie 设置在 analysis.kohleservices.com 域上
  → 父页面的 fetch() 请求可以携带新鲜的 CF_Authorization
```

**只在 HTTPS 环境下激活**（即通过 Cloudflare Tunnel 外网访问时）。内网 HTTP 访问不受影响。

### 代码改动

#### 1. 新建 `frontend/public/cf-refresh.html`

极简 HTML 页面，仅用于触发 CF Access 签发 token：

```html
<!DOCTYPE html><html><body></body></html>
```

#### 2. 修改 `frontend/src/layouts/DashboardLayout.tsx`

添加 `useCfTokenRefresh` hook：

```tsx
function useCfTokenRefresh(intervalMs = 8000) {
  useEffect(() => {
    // Only activate on HTTPS (Cloudflare external access)
    if (window.location.protocol !== "https:") return;

    const iframe = document.createElement("iframe");
    iframe.style.cssText = "display:none;width:0;height:0;border:0";
    iframe.setAttribute("aria-hidden", "true");
    document.body.appendChild(iframe);

    const refresh = () => {
      iframe.src = "/cf-refresh.html?t=" + Date.now();
    };
    refresh();
    const timer = setInterval(refresh, intervalMs);

    return () => {
      clearInterval(timer);
      iframe.remove();
    };
  }, [intervalMs]);
}
```

在 `DashboardLayout` 组件中调用：

```tsx
export default function DashboardLayout() {
  useCfTokenRefresh();
  // ... rest of component
}
```

#### 3. 后端 CORS 收紧（安全加固，非必须）

`backend/app/main.py` 中 CORSMiddleware 从 `allow_origins=["*"]` 改为读取 `settings.CORS_ORIGINS` 白名单。

```env
# backend/.env
CORS_ORIGINS=https://analysis.kohleservices.com,http://10.6.20.138:3000,http://10.6.20.138:5173,http://localhost:5173,http://localhost:3000
```

#### 4. Nginx API 限速（安全加固，非必须）

```nginx
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=30r/s;
location /api {
    limit_req zone=api_limit burst=50 nodelay;
    limit_req_status 429;
    add_header X-Content-Type-Options "nosniff" always;
    ...
}
```

### 部署步骤

1. 运行 `./deploy.sh` 重新构建生产容器
2. 外网打开 Dashboard，等待 10 秒以上
3. 点击 widget 刷新按钮，确认数据正常返回

### 注意事项

- iframe 每 8 秒加载一次轻量页面（~200 bytes），网络开销极小
- 业界称此模式为 **Silent Token Renewal**（OAuth2/OIDC 常用），但通常刷新间隔为 30-60 分钟，此处因 CF_Authorization 的 10 秒 TTL 需要更频繁刷新
- 如果未来 Cloudflare 修改 CF_Authorization 的 TTL 策略，可以调整 `intervalMs` 参数或移除此方案
- 如果需要更稳定的方案，可切换到方案 A（Bypass）或方案 C（Bypass + API Key）

## 相关文件

| 文件 | 说明 |
|------|------|
| `frontend/public/cf-refresh.html` | iframe token 续签用的轻量页面 |
| `frontend/src/layouts/DashboardLayout.tsx` | `useCfTokenRefresh` hook（iframe 定时刷新） |
| `frontend/nginx.conf` | Nginx 反向代理配置（`/api` → `api:8001`）+ API 限速 |
| `backend/app/main.py` | CORS 配置（白名单模式）|
| `backend/.env` | CORS_ORIGINS 环境变量 |
| `/etc/cloudflared/config.yml` | Cloudflare Tunnel 路由配置 |
| `docker-compose.prod.yml` | 生产环境容器编排 |
| `frontend/src/pages/Home.tsx` | Dashboard 首页（lazy load 所有 widget） |
| `frontend/src/components/dashboard/*.tsx` | 各 Dashboard widget 组件 |
| `backend/app/core/trace_middleware.py` | 请求追踪中间件（记录 prod log）|
| `backend/logs-prod/backend.log` | 生产环境后端日志 |
