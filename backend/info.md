### 总体建议（尽量少改、稳步演进）
- **微前端并存**（最小改动）  
  - 保留现有 Dash 服务作为数据与业务回调后端；新增一个独立的 React SPA（Vite + TS + Tailwind + shadcn/ui）。  
  - 前期将 React 页面以 iframe 嵌入现有 Dash 页面某处，逐步替换 Dash UI；后期再切换为独立前端 + 统一反向代理（Nginx）模式。  
  - 优点：与现网解耦、迁移可渐进、风险低；后端接口改动最少。

- **API 抽象**（面向 React 消费）  
  - 将 `data_loader` 里的查询封装为 Flask 路由（/api/...），输出 JSON（加 CORS）。  
  - 先复用现有查询与降采样策略（你们已经做了采样和TTL缓存，适合移动端）。  
  - React 用 React Query 做服务端状态缓存与请求重试（// resilient network caching）。

### Vite + Tailwind + shadcn/ui 可行性
- **完全可行**，推荐组合：Vite + React + TypeScript + Tailwind + shadcn/ui（基于 Radix UI）。  
- 注意点：  
  - 若非 iframe 方式嵌入到 Dash 页面，建议在 Tailwind 配置里设置 `prefix: 'tw-'`（// avoid CSS collision），避免与 Dash/Plotly/AgGrid 样式冲突。  
  - shadcn/ui 组件依赖 Radix 的样式变量，最好与 Tailwind 一起构建（// ensure consistent theming）。  
  - 图表采用 `react-plotly.js`，体验与现有 Plotly 最接近（// minimal chart logic rewrite）。

### 渐进式迁移路径（建议分阶段）
- **Phase 1（搭骨架）**  
  - 新建 `apps/web-frontend`（Vite 工程）；集成 Tailwind + shadcn/ui；接入 Auth0（见下）。  
  - 实现“登录页 + 主图表页壳”，用 React Query 拉取 `/api/trades`、`/api/ticks`。  
  - 通过 iframe 嵌入到 Dash 某页面，做真实数据联调。
- **Phase 2（功能对齐）**  
  - 用 AG Grid React 或 TanStack Table 替代 Dash 的表格（前者迁移成本最低）。  
  - 复制现有“时间选择/账号过滤/价差开关”等交互；图表用 react-plotly.js（// consistent experience）。
- **Phase 3（解耦上线）**  
  - Nginx 静态托管 React；`/api` 反向代理到 Flask/Dash；WebSocket 或 SSE 后补。  
  - 逐步移除 Dash 前端，仅保留 Dash 作为后端 API 层。

### Auth0 集成要点（前后端联动）
- **前端（SPA）**  
  - 使用 `@auth0/auth0-react` 或 `auth0-spa-js`，走 Authorization Code + PKCE。  
  - 仅在内存保存 Access Token（// avoid XSS via localStorage）；启用 Refresh Token Rotation。  
  - 配置允许回调/登出 URL（// auth0 application settings）。
- **后端（Flask/Dash）**  
  - 所有 `/api/*` 路由启用 Bearer JWT 校验（PyJWT/中间件；issuer、audience 校验必须开）。  
  - 对跨域前端，开启 CORS 且限制可信域名（// strict CORS policy）。  
  - 若仍保留少量基于 Dash 会话的路由，建议区分 API（JWT）与页面（会话/CSRF）两条线（// separate concerns）。

### 移动端 UI/UX 优化重点
- **适配与信息层次**  
  - Tailwind 响应式断点 + shadcn/ui 组件栈（List、Tabs、Sheet、Drawer）优化操作路径（// thumb-friendly）。  
  - 为移动端提供简化图表（后端继续降采样），默认只显示关键曲线，细节信息通过折叠或 Drawer 呈现。  
  - 表格用 AG Grid React 的移动友好配置或 TanStack Table + 虚拟滚动（// performance on low-end devices）。
- **登录页清晰化**  
  - 大标题 + 简要副文 + 单一 CTA；社交登录按钮显著；错误提示简洁（// reduce friction）。  
  - 骨架屏 + 渐进加载，弱网时给用户“正在校验会话”的明确反馈。

### 与现有 Dash 的协作注意
- **样式隔离**：优先 iframe（最稳）；若同页嵌入，Tailwind 前缀化 + CSS Modules/Scoping（// prevent style leakage）。  
- **数据一致性**：沿用你们后端 TTL 缓存 + 降采样；React 前端用 React Query 的 staleTime/suspense（// consistent freshness policy）。  
- **图表一致性**：继续用 Plotly（React 包装），减少重写成本。  
- **权限**：React 获取到的 Access Token 需要随请求发送；后端路由统一验签（// centralized auth enforcement）。

### 可落地的最小工作包（1-2 周冲刺）
- 搭 `apps/web-frontend`（Vite+TS+Tailwind+shadcn/ui+Auth0+React Query）。  
- 暴露 2-3 个核心读接口（trades/ticks/price-diff），Flask 层加 JWT 校验与 CORS。  
- React 主视图：日期选择、图表（react-plotly.js）、概要指标、简单表格。  
- 在 Dash 页面新增 iframe 指向 React 路由，做联调与比对。  
- 移动端登录页与主图基础适配。

- 结论
  - 用 React（Vite + Tailwind + shadcn/ui）在现有 Dash 框架旁路并存、渐进替换，技术上完全可行且风险低。  
  - Auth0 采用 SPA + 后端 JWT 校验的标准模式即可。  
  - 通过 iframe 或样式前缀化，避免 CSS 冲突；继续沿用 Plotly 与后端降采样策略，显著降低迁移成本。