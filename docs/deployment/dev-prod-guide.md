# 开发 & 生产环境部署指南

> 本指南讲解日常开发流程、生产环境部署、以及 Cloudflare Tunnel 的配置方式。服务器是内网的 Ubuntu 机器。

## 整体架构

```
Ubuntu 服务器 (10.6.20.138)
┌───────────────────────────────────────────────────────────────┐
│                                                               │
│  ┌─ 生产环境 (docker-compose.prod.yml) ──────────────────┐   │
│  │                                                       │   │
│  │  Nginx (:3000) ──→ FastAPI (内部:8001) ──→ Redis      │   │
│  │  预编译好的静态文件    代码打包进镜像                    │   │
│  │  速度快、稳定          不会自动重启                      │   │
│  └───────────────────────────────────────────────────────┘   │
│        ↑                                                      │
│   analysis.kohleservices.com（通过 Cloudflare Tunnel 访问）    │
│   http://10.6.20.138:3000（内网直接访问）                      │
│                                                               │
│  ┌─ 开发环境 (frontend & backend docker-compose.dev.yml) ┐   │
│  │                                                       │   │
│  │  Vite (:5173) ──→ FastAPI (:8001) ──→ Redis           │   │
│  │  挂载本地代码          改代码自动重启                    │   │
│  │  热更新、秒级刷新      修改即生效                        │   │
│  └───────────────────────────────────────────────────────┘   │
│        ↑                                                      │
│   http://10.6.20.138:5173（仅内网可访问）                      │
│                                                               │
│  ┌─ 其他服务 ────────────────────────────────────────────┐   │
│  │  :80  → blacklist-frontend-prod（csblacklist 域名）   │   │
│  │  :8000 → login_analysis_service（/ipmonitor/*）       │   │
│  └───────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────┘
```

---

## 开发 vs 生产环境对比

| | **开发环境** | **生产环境** |
|---|---|---|
| **前端** | Vite 开发服务器（实时编译） | Nginx（预打包好的静态文件） |
| **后端** | Uvicorn + `--reload`（改代码自动重启） | Uvicorn 2 个 worker（不自动重启） |
| **代码来源** | 从磁盘挂载（实时同步） | 构建时拷贝进 Docker 镜像 |
| **改了代码之后** | 浏览器自动刷新 | 不会生效，需要跑 `./deploy.sh` |
| **页面 HTTP 请求数** | 200-500 个（模块没打包） | 5-10 个（打包 + gzip 压缩） |
| **登录认证** | 关闭（`VITE_DISABLE_AUTH=true`） | 关闭（Cloudflare Zero Trust 负责认证） |
| **端口** | 前端 `:5173`，后端 `:8001` | Nginx `:3000`（统一入口，内部转发 `/api`） |
| **访问地址** | `http://10.6.20.138:5173` | `http://10.6.20.138:3000` 或 `analysis.kohleservices.com` |
| **用途** | 写代码、调试 | 给用户使用 |

---

## 访问方式

| 地址 | 看到的内容 | 走的路径 |
|---|---|---|
| `http://10.6.20.138:5173` | 开发版（实时代码） | 直连服务器，最快 |
| `http://10.6.20.138:3000` | 生产版（稳定构建） | 直连服务器，很快 |
| `https://analysis.kohleservices.com` | 生产版（稳定构建） | 浏览器 → Cloudflare 边缘节点 → Tunnel → `:3000` |

> **注意**：即使在内网，`analysis.kohleservices.com` 也是解析到 Cloudflare 的 IP（不是 `10.6.20.138`）。流量一定会走 Cloudflare。不过已经配了 bypass 规则，办公室 IP 段不需要登录认证。

---

## 日常开发流程

### 第一步：写代码（开发模式）

打开 Cursor，连到服务器，编辑代码。在浏览器打开 `http://10.6.20.138:5173` 查看效果。

改代码 → 浏览器自动刷新 → 马上看到效果。

### 第二步：部署到生产

改好了、测试没问题后：

```bash
# 方式 A：先提交代码，再部署（推荐）
git add . && git commit -m "feat: 你的描述" && git push origin main
./deploy.sh

# 方式 B：不提交，直接部署（用于测试生产构建）
docker compose -f docker-compose.prod.yml up -d --build
```

`deploy.sh` 做的事情：`git pull` → 重新构建镜像 → 重启生产容器。大约需要 20 秒。**开发环境的容器不受影响。**

### 第三步：验证

打开 `http://10.6.20.138:3000`，检查生产版本是否正常。

---

## Docker 容器一览

### 生产容器（`docker-compose.prod.yml`）

| 容器名 | 镜像 | 端口 | 说明 |
|---|---|---|---|
| `new-it-frontend-prod` | Nginx + 静态构建 | `3000:80` | 提供 React 页面 + 转发 `/api` |
| `new-it-backend-prod` | FastAPI (Uvicorn) | 仅内部 | 2 个 worker，不自动重启 |
| `new-it-redis-prod` | Redis 7 Alpine | 仅内部 | 生产缓存 |

### 开发容器

| 容器名 | compose 文件 | 端口 | 说明 |
|---|---|---|---|
| `new-it-frontend-dev` | `frontend/docker-compose.dev.yml` | `5173:5173` | Vite 开发服务器，热更新 |
| `new-it-backend-dev` | `backend/docker-compose.dev.yml` | `8001:8001` | Uvicorn + `--reload` |
| `new-it-redis` | `backend/docker-compose.dev.yml` | 仅内部 | 开发缓存 |

### 启动容器

```bash
# 启动开发环境（如果没跑起来的话）
cd /opt/myproject/New-IT-System/backend && docker compose -f docker-compose.dev.yml up -d
cd /opt/myproject/New-IT-System/frontend && docker compose -f docker-compose.dev.yml up -d

# 启动生产环境
cd /opt/myproject/New-IT-System && docker compose -f docker-compose.prod.yml up -d --build
```

### 常用命令

```bash
# 查看所有运行中的容器
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 查看生产日志
docker logs new-it-frontend-prod --tail 50
docker logs new-it-backend-prod --tail 50

# 只重启生产前端（比如只改了前端代码）
docker compose -f docker-compose.prod.yml up -d --build web

# 只重启生产后端
docker compose -f docker-compose.prod.yml up -d --build api
```

---

## 关键文件

| 文件 | 用途 |
|---|---|
| `docker-compose.prod.yml` | 生产环境编排文件（在项目根目录） |
| `frontend/docker-compose.dev.yml` | 开发前端（Vite） |
| `backend/docker-compose.dev.yml` | 开发后端（FastAPI + Redis） |
| `frontend/Dockerfile.prod` | 多阶段构建：Node（编译）→ Nginx（服务） |
| `frontend/Dockerfile` | 开发镜像（只有 Node，跑 Vite 开发服务器） |
| `backend/Dockerfile` | 后端镜像（Python，开发和生产共用） |
| `frontend/nginx.conf` | Nginx 配置：静态文件 + API 转发 + gzip 压缩 |
| `frontend/.dockerignore` | 排除 `node_modules`，加快 Docker 构建 |
| `deploy.sh` | 一键部署生产环境的脚本 |

---

## Cloudflare Tunnel 配置

Tunnel 以 systemd 服务运行，使用**本地配置文件**（不是 Cloudflare Dashboard 管理的）。

### 配置文件位置

```
/etc/cloudflared/config.yml
```

### 当前路由规则

```yaml
ingress:
  - hostname: csblacklist.kohleservices.com
    service: http://localhost:80          # 黑名单前端

  - hostname: analysis.kohleservices.com
    path: /dash/*
    service: http://10.6.20.138:8050     # Dash 服务（已停用）

  - hostname: analysis.kohleservices.com
    path: /ipmonitor/*
    service: http://10.6.20.138:8000     # IP 登录监控

  - hostname: analysis.kohleservices.com
    service: http://localhost:3000        # 主应用（生产 Nginx）

  - service: http_status:404             # 兜底：都不匹配就返回 404
```

规则是**从上往下匹配**的，第一个匹配到就生效。

### 修改 Tunnel 配置

```bash
sudo nano /etc/cloudflared/config.yml    # 编辑配置
sudo systemctl restart cloudflared       # 重启让配置生效
sudo systemctl status cloudflared        # 确认服务正常运行
```

> **重要**：这个 Tunnel 是通过命令行管理的。**不要**在 Cloudflare Zero Trust Dashboard 上改路由规则，会和本地配置文件冲突。

### DNS 解析说明

`analysis.kohleservices.com` 解析到的是 Cloudflare 的 IP（比如 `104.21.27.230`），不是内网服务器的 IP。所有流量都走 Cloudflare，即使在内网也是。已经配了 Access bypass 策略，办公室 IP 段免登录。

---

## 为什么生产比开发快很多（通过 Cloudflare 访问时）

| 因素 | 开发（Vite） | 生产（Nginx） |
|---|---|---|
| 每次页面加载的 HTTP 请求数 | 200-500 个 | 5-10 个 |
| 每个请求过 Cloudflare 的延迟 | ~100ms × 200 = **20 秒** | ~100ms × 5 = **0.5 秒** |
| Gzip 压缩 | 没有 | 有 |
| 静态资源缓存 | 不缓存 | 缓存 1 年（文件名带 hash） |
| WebSocket（热更新） | 有（但通过 Tunnel 可能不稳定） | 没有 |

---

## 常见问题排查

| 现象 | 原因 | 解决方法 |
|---|---|---|
| 生产环境显示登录页面 | 构建时没设 `VITE_DISABLE_AUTH` | 检查 `Dockerfile.prod` 里 `RUN npm run build` 之前有没有 `ENV VITE_DISABLE_AUTH=true`，然后重新构建 |
| `./deploy.sh` 在 `npm run build` 失败 | TypeScript 类型错误 | 先修好 TS 报错（开发模式下 Vite 不做类型检查，所以开发时不报错） |
| 端口 3000 被占用 | 有别的容器占了这个端口 | 用 `docker ps` 找到它，停掉或换端口 |
| Cloudflare 显示 "Authentication error" | 浏览器有过期的 cookie | 清除 `analysis.kohleservices.com` 的 cookie，或用无痕模式 |
| 改了 Tunnel 配置没生效 | `cloudflared` 没重启 | 跑 `sudo systemctl restart cloudflared` |
| 通过 Cloudflare 访问 API 返回 502 | 后端容器没在运行 | 用 `docker logs new-it-backend-prod` 看日志，有问题就重启 |
| Dashboard 首次加载正常，点刷新后 "Load failed" | Cloudflare Access 拦截了后续 fetch 请求 | 详见 [cloudflare-api-blocked.md](cloudflare-api-blocked.md)，需在 Access 中为 `/api/*` 添加 Bypass |
| Docker 构建很慢（500MB+ 上下文） | 缺少 `.dockerignore` | 确认 `frontend/.dockerignore` 里有 `node_modules` |
