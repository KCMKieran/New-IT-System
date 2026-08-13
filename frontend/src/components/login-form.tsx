import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/components/i18n-provider"

/**
 * Single sign-on entry point (auth design P3).
 *
 * This used to be the unmodified shadcn template: an email/password pair that
 * accepted anything, plus three links that went nowhere (Forgot password,
 * Login with GitHub, Sign up). There is exactly one way into this system now —
 * the company Entra ID (Azure AD) directory — so there is exactly one control.
 * Password fields would be a lie: no password is ever checked here.
 */

type LoginFormProps = React.ComponentProps<"div"> & {
  onSignIn: () => void
  /** Short code from `/login?error=…`, already mapped to a message by the page. */
  errorMessage?: string | null
  busy?: boolean
}

function MicrosoftLogo() {
  // The official four-square mark. Fixed brand colours in both themes — this is
  // Microsoft's logo, not our palette, and recolouring it is off-limits.
  return (
    <svg viewBox="0 0 23 23" className="size-4" aria-hidden="true">
      <path fill="#f25022" d="M1 1h10v10H1z" />
      <path fill="#7fba00" d="M12 1h10v10H12z" />
      <path fill="#00a4ef" d="M1 12h10v10H1z" />
      <path fill="#ffb900" d="M12 12h10v10H12z" />
    </svg>
  )
}

export function LoginForm({
  className,
  onSignIn,
  errorMessage,
  busy = false,
  ...props
}: LoginFormProps) {
  const { t } = useI18n()

  return (
    <div className={cn("flex flex-col gap-6", className)} {...props}>
      <div className="flex flex-col items-center gap-2 text-center">
        <h1 className="text-2xl font-bold">{t("auth.title")}</h1>
        <p className="text-muted-foreground text-sm text-balance">
          {t("auth.subtitle")}
        </p>
      </div>

      {errorMessage ? (
        <div
          role="alert"
          className="border-destructive/40 bg-destructive/10 text-destructive rounded-md border px-3 py-2 text-sm"
        >
          {errorMessage}
        </div>
      ) : null}

      <Button
        type="button"
        variant="outline"
        className="w-full"
        onClick={onSignIn}
        disabled={busy}
      >
        <MicrosoftLogo />
        {t("auth.signInWithMicrosoft")}
      </Button>

      <p className="text-muted-foreground text-center text-xs text-balance">
        {t("auth.accessNote")}
      </p>
    </div>
  )
}
