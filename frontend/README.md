# KCM IT System - Frontend

React-based frontend for the KCM IT System analytics platform.

## Tech Stack

| Technology | Version | Purpose |
|------------|---------|---------|
| React | 18.x | UI framework |
| TypeScript | 5.x | Type safety |
| Vite | 6.x | Build tool & dev server |
| shadcn/ui | - | UI component library |
| Tailwind CSS | 4.x | Styling |
| AG-Grid | 34.x | Data tables with server-side pagination |

## Quick Start

```bash
# Install dependencies
npm install

# Start development server
npm run dev
# Opens at http://localhost:5173

# Build for production
npm run build

# Preview production build
npm run preview
```

## Project Structure

```
frontend/
├── src/
│   ├── main.tsx           # App entry point
│   ├── App.tsx            # Root component with routing
│   ├── index.css          # Global styles (Tailwind)
│   ├── pages/             # Page components
│   │   ├── Position.tsx   # Real-time position monitor
│   │   ├── ClientPnLAnalysis.tsx
│   │   ├── IBReport.tsx
│   │   └── ...
│   ├── components/        # Reusable components
│   │   ├── ui/            # shadcn/ui components
│   │   ├── site-header.tsx
│   │   └── ...
│   ├── providers/         # Context providers
│   │   └── auth-provider.tsx
│   └── lib/               # Utilities
│       └── utils.ts
├── public/                # Static assets
├── index.html             # HTML template
├── vite.config.ts         # Vite configuration
├── tailwind.config.js     # Tailwind configuration
└── components.json        # shadcn/ui CLI config
```

## Key Pages

| Page | Route | Description |
|------|-------|-------------|
| Position Monitor | `/position` | Real-time open positions across MT4/MT5 |
| Client PnL | `/client-pnl` | Customer profit/loss analysis |
| IB Report | `/ib-report` | Broker commission reports |
| Equity Monitor | `/equity` | Account equity tracking |
| Trade Summary | `/trade-summary` | Trading volume aggregation |

## Development Guide

### Adding a New Page

1. Create page component in `src/pages/MyPage.tsx`
2. Add route in `src/App.tsx`:
   ```tsx
   <Route path="/my-page" element={<MyPage />} />
   ```
3. Add to sidebar navigation in `src/components/app-sidebar.tsx`
4. Update title mapping in `src/components/site-header.tsx`:
   ```tsx
   const titleMap = {
     "/my-page": "My Page Title",
     // ...
   }
   ```

### Adding shadcn/ui components (Docker 环境)

当使用 Docker 跑前端时，**不要在容器内**执行 `npx shadcn@latest add xxx`。应在**宿主机**上执行，使新组件文件写入源码目录：

```bash
cd frontend
npx shadcn@latest add <component-name>   # 例如: collapsible
```

开发时 `docker-compose.dev.yml` 通过 volume 挂载 `.:/app`，宿主机新增的 UI 组件会同步到容器；构建生产镜像时这些文件会随源码一起 COPY 进镜像。详见 [Dashboard 近两日净盈亏](docs/features/dashboard-pnl24h-by-country-sql.md) 文档 §8。

### Using AG-Grid

See [AG-Grid Integration Guide](../docs/frontend/ag-grid-integration.md) for:
- Module registration
- Theme configuration
- Server-side pagination
- Common troubleshooting

### Theme Support

The app supports light/dark themes:
- Theme provider in `src/components/theme-provider.tsx`
- Toggle in `src/components/mode-toggle.tsx`
- AG-Grid theme switches automatically with `ag-theme-quartz` / `ag-theme-quartz-dark`

## Environment Variables

Create `.env.development` or `.env.production`:

```env
# API backend URL
VITE_API_BASE_URL=http://localhost:8001

# Skip authentication (dev only)
VITE_DISABLE_AUTH=true
```

## Documentation

- [File Structure Explained](../docs/frontend/file-structure.md)
- [AG-Grid Integration](../docs/frontend/ag-grid-integration.md)
- [Filter Module Design](../docs/frontend/filter-module-design.md)
- [Full Documentation](../docs/)
