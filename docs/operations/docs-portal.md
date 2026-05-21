# 文档中心 / Docs Portal — 运维与开发指南

> 入口：sidebar `Configuration → 文档中心` 或直接 `https://analysis.kohleservices.com/docs/`（生产）/ `http://10.6.20.138:3000/docs/`（容器直连）。
> OPT 来源：[`docs/optimization/items/OPT-0026-docs-portal.md`](../optimization/items/OPT-0026-docs-portal.md)。

## 1. 架构一图

```
┌───────────────────┐    Cloudflare    ┌─────────────────┐
│ iPad / 手机浏览器 │ ─── Tunnel ────▶ │ Nginx (web 容器) │
└───────────────────┘                  └──────┬──────────┘
                                              │ /docs/* → proxy_pass
                                              │ http://mkdocs:8000/
                                              ▼
                                       ┌──────────────────┐
                                       │ mkdocs container │
                                       │ (mkdocs serve)   │
                                       │  ↑ 挂载 ./docs/  │
                                       │  ↑ 挂载 mkdocs.yml│
                                       └──────────────────┘
```

- **入口 URL**：`/docs/`（子路径，复用主站的 Cloudflare Tunnel + Nginx，零额外 DNS/证书）。
- **后端**：`mkdocs serve --dev-addr=0.0.0.0:8000` 跑在 `mkdocs` service 里（image `squidfunk/mkdocs-material:latest`）。
- **挂载**：`./docs:/docs/docs:ro` + `./mkdocs.yml:/docs/mkdocs.yml:ro`，**只读** —— 容器永远改不到源文件。
- **链接前缀**：mkdocs.yml `site_url: .../docs/` 让生成的 HTML 内部链接带 `/docs/` 前缀；Nginx `proxy_pass http://mkdocs:8000/`（带 trailing slash）剥掉前缀再转发给 mkdocs，所以上游收到的是它认识的根路径。

## 2. 本地启动 / 重启

### Prod
docs 站随 `./deploy.sh` 一起起：
```bash
./deploy.sh
docker ps | grep new-it-mkdocs-prod   # 验证容器在跑
```

只重启 docs 容器（不影响 frontend / api）：
```bash
docker compose -f docker-compose.prod.yml restart mkdocs
```

查日志：
```bash
docker logs -f new-it-mkdocs-prod
```

### Dev
不在 `frontend/docker-compose.dev.yml` 里，要单独起：
```bash
docker compose -f docker-compose.prod.yml up -d mkdocs
# 然后通过 prod nginx 访问 http://10.6.20.138:3000/docs/
```

或者直接 host 上跑 mkdocs（不走 Docker）：
```bash
pip install mkdocs-material
mkdocs serve --dev-addr=0.0.0.0:8000
# 访问 http://10.6.20.138:8000/  注意没有 /docs/ 前缀
```

## 3. 怎么改导航 / 加新页

MkDocs 默认按 **`docs/` 目录结构** 自动生成左侧导航——只要新建 `.md` 文件，刷新页面就能看到，**不用改 `mkdocs.yml`**。

文件名规则：
- 顶层一级章节用目录名（`docs/operations/` → "Operations" 一栏）
- 想自定义某页标题：在 markdown 文件顶部加 `# 我的标题`
- 想排除某目录：在 `mkdocs.yml` 的 `exclude_docs:` 加路径（当前只排除了 `analysis/`，理由：是原始数据快照不是文档）

## 4. Mermaid 图

直接在 markdown 里写：

````markdown
```mermaid
graph LR
    A[Trader] --> B[Risk Engine]
    B --> C{Rule fired?}
    C -->|yes| D[Alert + SSE push]
    C -->|no| E[Discard]
```
````

渲染靠 mkdocs-material 9+ 内置的 Mermaid 集成 + `mkdocs.yml` 的 `pymdownx.superfences` custom_fences 配置——主题会在含 `.mermaid` 块的页面上自动注入 runtime，无需额外 CDN 或 init JS。

## 5. 排障

| 症状 | 可能原因 | 解决 |
|---|---|---|
| `502 Bad Gateway` 访问 `/docs/` | mkdocs 容器没起 | `docker compose -f docker-compose.prod.yml up -d mkdocs` |
| 页面 404 但文件存在 | mkdocs serve 启动时报错 | `docker logs new-it-mkdocs-prod` 看 yaml 解析错 |
| 改 .md 后页面没刷新 | livereload ws 没穿过 nginx | 浏览器手动刷新即可；或 `docker restart new-it-mkdocs-prod` |
| 内部链接 404 / 样式坏 | site_url 和 nginx proxy 前缀不一致 | 确认 `mkdocs.yml site_url` 末尾有 `/` 且 nginx `proxy_pass` URL 末尾也有 `/` |
| Mermaid 图不渲染 | 客户端没载 mermaid.js | 检查浏览器 console；CDN 被墙时改用本地 mermaid.min.js |
| frontmatter YAML 显示成代码 | 文件第一行不是 `---` | MkDocs 自动剥 YAML 块的前提是文件**首行**就是 `---` |

## 6. 安全提醒

⚠ 这是**内部** docs 站，含 SQL schema、内网 IP、API 密钥引用、风控规则等敏感信息。开 Cloudflare Tunnel 公网前确认：

1. Cloudflare Access 已经把 `analysis.kohleservices.com` 网段保护起来（email/SSO 鉴权）—— `/docs/` 子路径自动继承。
2. **不要**在 docs 里直接写明文密码/token。引用 `.env` 文件而不是粘贴值。
3. `docs/lessons/` 目录是 gitignored 的本地教学资产；如果你不想公开，加到 `mkdocs.yml exclude_docs:`。

## 7. 不动它的部分

- **`docs/` 目录结构**：尽量稳定，因为是 markdown 之间互相 `[相对链接](../foo/bar.md)` 的根。改动结构 = 大量链接需更新。
- **`docs/optimization/`**：是 OPT 流程的源数据（`backlog.md` / `items/*.md`），workflow 工具读它，**不要**让 MkDocs 修改这些文件——挂载已经是 `:ro` 保证这点。

## 8. 未来增强（不在 OPT-0026 范围）

- 切到 `mkdocs build` + Nginx 直接 serve 静态文件（性能更高，但 docs 改动需要重 build）
- 加 `mkdocs-awesome-pages-plugin` 让导航顺序自定义
- 加全文搜索 highlight + 中文分词（jieba）
- 用 git hook 在 `docs/**/*.md` 改动时自动 rebuild
