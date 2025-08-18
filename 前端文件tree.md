下面按层次梳理 `frontend` 目录中常见文件/目录的用途与它们之间的关系（结合你当前项目结构与 Vite/React 的惯例）。

### 运行/渲染链路（最重要）
- 1) `index.html`（项目根目录）  
  - 唯一的 HTML 模板。包含一个 `id="root"` 的容器。Vite 在开发/构建时把脚本注入这里。
- 2) `src/main.tsx`  
  - 前端入口。挂载 React 应用到 `#root`，导入全局样式（`src/index.css`），并包裹顶层 Providers（如 `ThemeProvider`）。
- 3) `src/App.tsx`  
  - 根组件。决定“首页看到什么”，再组合使用各种业务/UI组件（如 Sidebar、Header、Charts 等）。
- 4) 组件与样式  
  - `src/components/**` 放组件；`src/index.css` 放全局样式与 Tailwind 指令；`src/App.css`（若被导入）只影响 `App` 内部样式。

小结：浏览器加载 `index.html` → 执行 `src/main.tsx` → 渲染 `src/App.tsx` → 页面显示组件树。

### `src` 目录关键文件
- `src/main.tsx`：应用入口，创建根节点、挂载 `<App />`，引入全局 CSS，放置全局 Provider（如主题、路由、状态等）。
- `src/App.tsx`：根组件。当前已集成 Sidebar、Header、统计卡片、图表、主题切换等。
- `src/index.css`：全局样式（Tailwind 指令、CSS 变量、主题色、基础层 `@layer base`）。影响全站。
- `src/App.css`：仅当被显式 `import './App.css'` 时生效；通常用于 `App` 组件的局部样式。你当前代码未引入，等于未使用。
- `src/components/**`：可复用组件。  
  - `components/ui/*`：UI 基础组件（Button、Sidebar、Card 等）。  
  - `components/theme-provider.tsx`：主题上下文/状态管理（light/dark/system）。  
  - `components/mode-toggle.tsx`：点击切换主题的按钮。
- `src/lib/utils.ts`：工具函数（如 `cn` 合并 className）。
- `src/assets/*`：静态资源（如 `react.svg`）。
- `src/app/**`：有些 CLI 会把示例/页面数据放这里（如 `app/dashboard/data.json`）。

### 配置类文件（根目录）
- `index.html`：单页应用模板，包含 `#root` 容器。Vite 会注入模块脚本，启动你的 `src/main.tsx`。
- `vite.config.ts`：Vite 构建/开发配置（插件、别名、服务器配置等）。
- `tsconfig.json`：TypeScript 根配置（如路径别名 `@/*`）。通常用作“聚合与共享”配置。
- `tsconfig.app.json`：给前端运行代码用的 TS 配置（开启 JSX、DOM lib、bundler 模块解析等）。
- `tsconfig.node.json`：给 Node 环境文件用（如 `vite.config.ts`），开启 Node/ESNext 能力与额外选项。
- `vite-env.d.ts`：Vite 提供的类型声明（如 `import.meta.env`），让 TS 识别 Vite 特性。
- `package.json`：项目元信息与依赖、脚本（`npm run dev/build/preview`）。  
- `package-lock.json`：依赖锁定文件，保证安装版本一致。
- `eslint.config.js`：ESLint 规则（代码质量/风格）配置。
- `components.json`：shadcn UI 的 CLI 配置（组件生成偏好、路径、样式系统等）。不用 CLI 时可忽略。

### 资源与静态文件
- `public/`：静态公开资源目录，构建时会原样复制到输出根目录（通过 `/xxx.ext` 直接访问）。
- `src/assets/`：通过打包器（Vite）处理的资源（可被 import、会走哈希指纹等）。

### 它们之间的关系与常见疑问
- 全局 vs 局部样式  
  - `index.css` 是全局样式基础；`App.css` 是可选的局部样式（需要在 `App.tsx` 手动导入才生效）。
- 为什么执行 CLI 后页面没变  
  - CLI 只“生成/安装组件代码”，不会自动修改你的 `App.tsx`。需要手动在 `App.tsx` import 并使用它们（我已帮你接好）。
- 别名 `@/*` 从哪里来  
  - 在 `tsconfig.json`/`tsconfig.app.json` 里配置 + `vite.config.ts` 插件支持，简化 `import` 路径（如 `@/components/...`）。
- 路由在哪里  
  - 当前项目未使用路由库（如 `react-router`）。若需要多页切换，需要引入路由并在 `App.tsx` 里配置 `<Routes>`。

### 给新手的实践建议
- 页面“入口”找 `src/main.tsx`，页面“内容”找 `src/App.tsx`。
- 全局样式放 `src/index.css`；组件样式尽量用 Tailwind + 可复用 UI 组件。
- 新引入的组件/页面，只有在 `App.tsx`（或路由）挂上后才会显示。
- 改配置（TS/Vite）后，若类型或路径报错，优先检查 `tsconfig.*` 与 `vite.config.ts` 是否匹配。

- 重点：
  - 渲染链路：`index.html` → `src/main.tsx` → `src/App.tsx`
  - 全局样式：`src/index.css`，`App.css` 仅在被导入时生效
  - 组件来源：`src/components/**`；主题相关放在 `theme-provider.tsx` 与 `mode-toggle.tsx`
  - 配置：`tsconfig.*`、`vite.config.ts`、`components.json`、`eslint.config.js` 等用于类型、构建、生成组件和代码质量