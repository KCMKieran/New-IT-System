import { createContext, useContext, useEffect, useState, type ReactNode } from "react"

// Supported theme options for the app
type Theme = "dark" | "light" | "system"

// Props for the ThemeProvider component
type ThemeProviderProps = {
  children: ReactNode
  defaultTheme?: Theme
  storageKey?: string
}

// Context state shape so that consumers can read and set the theme
type ThemeProviderState = {
  theme: Theme
  setTheme: (theme: Theme) => void
}

// Default (safe) initial state used before the provider mounts
const initialState: ThemeProviderState = {
  theme: "system",
  setTheme: () => null,
}

const ThemeProviderContext = createContext<ThemeProviderState>(initialState)

export function ThemeProvider({
  children,
  defaultTheme = "system",
  storageKey = "vite-ui-theme",
  ...props
}: ThemeProviderProps) {
  // Initialize from localStorage or fallback to default
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem(storageKey) as Theme) || defaultTheme)

  // Apply the theme by toggling classes on the <html> element
  useEffect(() => {
    const root = window.document.documentElement
    root.classList.remove("light", "dark")

    if (theme === "system") {
      const systemPrefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches
      root.classList.add(systemPrefersDark ? "dark" : "light")
      return
    }

    root.classList.add(theme)
  }, [theme])

  // Persist choice to localStorage for future visits
  const value: ThemeProviderState = {
    theme,
    setTheme: (nextTheme: Theme) => {
      localStorage.setItem(storageKey, nextTheme)
      setTheme(nextTheme)
    },
  }

  return (
    <ThemeProviderContext.Provider {...props} value={value}>
      {children}
    </ThemeProviderContext.Provider>
  )
}

// Hook to consume the ThemeProvider context safely
export const useTheme = () => {
  const context = useContext(ThemeProviderContext)
  if (context === undefined) throw new Error("useTheme must be used within a ThemeProvider")
  return context
}


