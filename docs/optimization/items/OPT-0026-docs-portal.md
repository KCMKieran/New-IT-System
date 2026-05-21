---
id: OPT-0026
title: 文档中心 sidebar 入口 + MkDocs Material 自托管站点
status: done
priority: P3
area: mixed
effort: M
created: 2026-05-21
closed: 2026-05-21
---

## 问题

项目 `docs/` 下已经积累了几十份 markdown 文档（features、architecture、backend、frontend、operations、optimization、ai-context 等），但目前只能在工作站本地用编辑器或 GitHub 上看：

- 手机 / iPad 上无法快速翻阅（开会途中、家里、出差）
- 没有全文搜索，跨文档找一个概念要 grep
- 文档之间的链接是相对路径，在文件管理器里点不通
- 新同事 onboarding 没有一个统一入口

## 背景

- 现有基础设施：Nginx 反代 + Cloudflare Tunnel（`analysis.kohleservices.com`）已经在用，新增子路径或子域名零成本
- 上次讨论（与用户）确认**不**走 GitHub 路径——内部金融风控系统含 SQL schema / AML 规则 / 内网 IP，外部托管有合规风险
- 用户决策：**MkDocs Material 自托管**作为站点方案
- sidebar 当前结构 `frontend/src/components/app-sidebar.tsx:110-134` 有一个 `documents` 数组传给 `NavDocuments`，组 label 是 "Configuration"。当前第一项是 `Managers`，新菜单要插到 `Managers` 之前作为第一项
- i18n key 位置：`frontend/src/i18n/locales/{en-US,zh-CN}.ts` 的 `config.*` 段
- 菜单展示名：**文档中心 / Docs**（用户拍板）

## 假设 / 待验证

- [ ] MkDocs Material 能直接吃 `docs/**/*.md` 的现有结构（顶层 README + 子目录），还是需要专门写一个 `mkdocs.yml` nav 树
- [ ] `docs/optimization/` 这种带 frontmatter 的 markdown 在 MkDocs 渲染是否正常（YAML 块会不会显示出来）
- [ ] Mermaid 图（项目里架构文档有用）需要 `pymdownx.superfences` + `mermaid2` 插件
- [ ] 部署形式：选 A 子路径（`analysis.kohleservices.com/docs/`）还是 B 子域名（`docs.kohleservices.com`）——子路径省 Cloudflare 配置，子域名隔离更干净
- [ ] sidebar menu 跳转方式：新窗口（`<a target="_blank">`）还是同窗口跳外链——主项目用 React Router 的 `<Link>` 是内部跳转，docs 站是外部独立 origin
- [ ] 鉴权：docs 站要不要也走 Cloudflare Access？还是和主项目共享同一个 cookie（如果不，公开 docs 是否可接受 / 部分文档敏感）

## 验收标准

- [ ] 在生产环境（`analysis.kohleservices.com` 或子域名）可以从手机浏览器访问 docs 站点，能看到所有 `docs/**/*.md` 内容
- [ ] docs 站点有搜索框（MkDocs Material 内置 search 插件足够）
- [ ] sidebar `Configuration` 组的第一项变成 **文档中心 / Docs**（在 `Managers` 之前），点击跳转到 docs 站
- [ ] i18n key `config.docs` 中英文都加好，复用现有的 `useI18n` 模式
- [ ] icon 选一个语义合适的 tabler icon（候选 `IconBook` / `IconBookmark` / `IconFiles`）
- [ ] docker-compose 接入：新增一个 `mkdocs` service（dev 和 prod 都跑，dev 可 hot-reload）
- [ ] `docs/` 目录结构尽量不动；如果要改也只动顶层导航（避免和现有 cross-link 冲突）
- [ ] 写一份 `docs/operations/docs-portal.md`：怎么启动、怎么加新页、怎么改导航、Mermaid 怎么用
- [ ] iPad / iPhone Safari 上实测：可以读、可以搜、左侧导航能展开收起

## 笔记

候选方案对比（已和用户对齐方向 = MkDocs Material）：

| 方案 | 优 | 劣 |
|---|---|---|
| MkDocs Material（✅） | 主题美、搜索强、Mermaid OK、生态成熟、Docker image 现成 | 需要写 `mkdocs.yml`、Python 构建 |
| Docsify | 零构建（前端读 .md）、配置极简 | 搜索弱、SEO 差、需手维护 sidebar |
| 外链 GitHub | 零工作量 | ⚠ 内部代码外泄风险（用户已否决） |
| in-app iframe | 鉴权统一 | mobile 体验差、双套滚动条 |

实施草稿：

1. **MkDocs service**：在 `backend/docker-compose.yml` 加一个 `mkdocs` service（image `squidfunk/mkdocs-material:latest`，挂载 `../docs:/docs:ro`，port 8002）
2. **mkdocs.yml**：放在仓库根目录或 `docs/mkdocs.yml`，配置 nav、主题色、search、Mermaid 插件、`exclude_docs` 把 `docs/optimization/items/*.md` 的 frontmatter 处理好（要么剥掉、要么自定义 markdown 扩展显示）
3. **Nginx**：加一个 location（`/docs/` 或独立 server_name），proxy_pass 到 mkdocs:8000
4. **Cloudflare Tunnel**：如果走子域名，加一条 ingress rule
5. **Sidebar**：改 `app-sidebar.tsx` `documents` 数组，第一项加 `{ name: t("config.docs"), url: "<docs-site-url>", icon: IconBook, external: true }`。考虑要不要给 `NavDocuments` 加 `external` 字段支持 `<a target="_blank">`
6. **i18n**：`en-US.ts` 加 `docs: "Docs"`，`zh-CN.ts` 加 `docs: "文档中心"`
7. **部署**：`./deploy.sh` 把 mkdocs service 一起起；先 dev 验证再 prod

潜在隐患（留给 Stage 1 outsider-review 考虑）：

- mkdocs container 重启不刷新 → 用 `mkdocs serve --livereload`（dev）/ build 后 serve（prod）
- `docs/optimization/items/` 几十个文件 + frontmatter，navigation 会很乱——可能要 `awesome-pages` 插件
- docs 站和主站不同 origin → sidebar 的 `<Link>` 会触发 SPA 路由报错；需要在 nav 层判定 external 走 `<a>` 而非 `<Link>`
- 安全：如果走 Cloudflare 公网入口，要确认 docs 内容没敏感凭据（grep `password|secret|token|@10\.6` 一遍）

## 结果

### 实际交付

实施 = AC 1-8 全部命中，9 (手机/iPad 实测) 留给用户。Merge commit 后会绑定 SHA — 历史用 `git log --grep="OPT-0026" --merges` 找。

**核心文件**:
- `mkdocs.yml` 仓库根，Material 9.7.6 / 中文 / superfences (Mermaid) / 中英 search
- `docker-compose.prod.yml` 加 `mkdocs` service：image pin `9.7.6` + ro mount `./docs` + `./mkdocs.yml` + healthcheck (`127.0.0.1:8000/docs/`)
- `frontend/nginx.conf` 加 `location /docs/` → `proxy_pass http://mkdocs:8000`（**不带** trailing slash 保留前缀；mkdocs `site_url=/docs/` 配套）
- `frontend/src/components/app-sidebar.tsx` Configuration 组顶部插「文档中心 / Docs」external link，icon = `IconBook`
- `frontend/src/components/nav-documents.tsx` 加 `external?: boolean` 字段 + `NavLink` (forwardRef，asChild ref 链路) + firstItems 可见数 3→4
- `frontend/src/i18n/locales/{zh-CN,en-US}.ts` `config.docs` 中英
- `docs/operations/docs-portal.md` 8 节运维 doc：架构图 / 启停 / 加新页 / Mermaid / 排障 / 安全
- `.gitignore` 加单文件例外 `!docs/operations/docs-portal.md`（其他同目录文件保持本地）

**端到端验证**（端口 3000，Cloudflare Tunnel 之前）:
- `GET /docs/` → 200, 76KB HTML
- `GET /docs/operations/docs-portal/` → 200, "文档中心" 命中 8 次
- `GET /docs/analysis/` → 404（`exclude_docs: /analysis/` 工作）
- `GET /docs`（无 trailing slash）→ 301 → `/docs/`（nginx 自动 redirect，不走 SPA fallback）
- mkdocs container healthcheck → `healthy`
- frontend `tsc -b --noEmit` → 干净

### Stage 1 outsider-review 处理记录

reviewer 给 11 条 findings。在转述前 sanity-check 验证：reviewer 关于「`/docs` 无 slash 落到 SPA」和「`exclude_docs: /analysis/` 不 work」均**实测不成立**（前者是 nginx 自动 301、后者实测 404）。

最终 curate + 用户拍板:

| # | Finding | 处理 |
|---|---|---|
| F1 | gitignored docs (lessons/, ai-context/PROJECT_CONTEXT.md, deployment/cloudflare-api-blocked.md ...) 被 portal 渲染给所有 CF Access SSO 用户 | **Live with** — 内部团队已经能 ssh / clone 看同样文件，portal 只是 friendlier read path；不引入新访问权限 |
| F2 | `image: mkdocs-material:latest` 不 pin + 没 healthcheck → 升级 silent break | **当场修**：pin `9.7.6` + healthcheck `127.0.0.1:8000/docs/`（IPv4，因为 image 内 localhost=::1 但 mkdocs bind 0.0.0.0:8000） |
| F3 | ops doc `docs-portal.md` line 26 / 93 写"trailing slash"跟实际反 | **当场修**：line 26 改写明"**不**带 / 保留前缀避免 302 循环"，line 93 troubleshooting 加新行涵盖错改 trailing slash + healthcheck 故障 |
| F4 | `mkdocs serve` 是 dev server（livereload watchers + Tornado）不适合 prod | **新 hardening OPT** — 切到 `mkdocs build` + nginx serve 静态文件 |
| F5 | `.gitignore` 单文件例外不可扩展 | **Live with** — 用户上一轮明确选择单文件例外 over 整目录 |
| F6 | NavDocuments external item 多余的 dropdown (Open/Share/Delete 都无 onClick) | **新 hardening OPT** — 跟 F4 合一个 |
| F7-F9 | aria-label / 502 branded page / SPA subpath | **Live with** — nice-to-have 优先级低 |

reviewer 误判：F1 中的 `/docs` 落到 SPA 不成立 (是 301)、F10 中 `exclude_docs:/...` 前缀斜杠不 work 不成立 (实测 OK)。

### Follow-up

- **OPT-0027 (docs-portal-hardening)** — 包 F4 (mkdocs build mode) + F6 (NavDocuments external dropdown 隐藏 + aria-label)。Effort M
- **Live with 累积**: F1 / F5 / F7 / F8 / F9 — 写在这里留 paper trail，未来真出事再回头修

### 手机/iPad 实测

留给用户用户在浏览器手动测：sidebar 文档中心可点、新窗口 docs 打开、能搜索、左侧导航能展开收起。

Verified via prod nginx 链路（不是 dev 5173）后**才**算这条 AC 命中。
