/**
 * Shared AG-Grid CSS-variable overrides for the Fund Flow Monitor page.
 *
 * Lifted from RiskMonitor.tsx — keeps the header dark-on-light (light theme)
 * and light-on-dark (dark theme), and binds row background / border to the
 * shadcn CSS variables so the grid sits flush with surrounding Cards.
 */

export function useGridThemeStyle(isDarkMode: boolean) {
  return {
    ["--ag-header-background-color" as string]: isDarkMode
      ? "hsl(0 0% 100% / 1)"
      : "hsl(0 0% 8% / 1)",
    ["--ag-header-foreground-color" as string]: isDarkMode
      ? "hsl(0 0% 0% / 1)"
      : "hsl(0 0% 100% / 1)",
    ["--ag-header-column-separator-color" as string]: isDarkMode
      ? "hsl(0 0% 0% / 1)"
      : "hsl(0 0% 100% / 1)",
    ["--ag-header-column-separator-width" as string]: "1px",
    // Compact header/cell horizontal padding (quartz default 16px)
    ["--ag-cell-horizontal-padding" as string]: "4px",
    ["--ag-background-color" as string]: "hsl(var(--card))",
    ["--ag-foreground-color" as string]: "hsl(var(--foreground))",
    ["--ag-row-border-color" as string]: "hsl(var(--border))",
    ["--ag-odd-row-background-color" as string]: isDarkMode
      ? "rgba(255,255,255,0.04)"
      : "rgba(0,0,0,0.03)",
  };
}
