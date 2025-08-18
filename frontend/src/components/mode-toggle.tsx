import { Moon, Sun } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useTheme } from "@/components/theme-provider"

// Simple toggle button between light and dark (cycles through system as well)
export function ModeToggle() {
  const { theme, setTheme } = useTheme()

  // Click handler toggles among light -> dark -> system -> light
  function handleClick() {
    if (theme === "light") return setTheme("dark")
    if (theme === "dark") return setTheme("system")
    return setTheme("light")
  }

  return (
    <Button variant="outline" size="icon" onClick={handleClick} aria-label="Toggle theme">
      <Sun className="h-[1.2rem] w-[1.2rem] scale-100 rotate-0 transition-all dark:scale-0 dark:-rotate-90" />
      <Moon className="absolute h-[1.2rem] w-[1.2rem] scale-0 rotate-90 transition-all dark:scale-100 dark:rotate-0" />
      <span className="sr-only">Toggle theme</span>
    </Button>
  )
}


