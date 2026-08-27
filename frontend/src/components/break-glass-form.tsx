import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { IconAlertTriangle } from "@tabler/icons-react"

/**
 * Emergency sign-in, reachable only at `/login?break_glass=1` (auth design
 * §4.2.2, prerequisite 2).
 *
 * It exists so that an Entra outage — the IdP down, an expired client secret, a
 * broken OIDC path — never has to be answered with `AUTH_ENABLED=false`, which
 * since Cloudflare Access was retired opens the entire API to anyone holding
 * the API key that Vite compiles into this bundle. Here the session layer, the
 * module gate and the audit trail all stay on; only the hop to Microsoft is
 * skipped, and the session minted this way expires in hours rather than days.
 *
 * ⚠ Deliberately NOT linked from the normal login screen, and deliberately not
 * advertised by /auth/me. The backend answers 404 whenever the mode is not
 * armed, so an outsider cannot tell the door from a wall — and telling them
 * there is a door with a shared secret behind it is an invitation to grind it.
 * The URL lives in the runbook (design §5.5); it is not a secret, but it is not
 * a signpost either.
 *
 * ⚠ Not internationalised, same reasoning as NoModules.tsx: the people who
 * reach this screen are two or three named operators during an incident, and
 * the text is an instruction to act on, not product copy. English + Chinese
 * inline is more robust than an i18n bundle that has to load first — and during
 * an outage "the page rendered but the strings did not" is a failure mode worth
 * designing out.
 */

type Props = {
  /** Resolves to an error code, or null once a session exists. */
  onSubmit: (email: string, secret: string) => Promise<string | null>
}

const MESSAGES: Record<string, string> = {
  denied:
    "Refused. Check the address is on AUTH_BREAK_GLASS_EMAILS, that the account already exists and is active, and that the secret matches. 已拒绝：请确认邮箱在应急名单内、账号已存在且未停用、密钥正确。",
  unavailable:
    "Break-glass mode is not armed on the server. Set AUTH_BREAK_GLASS_ENABLED / _EMAILS / _SECRET in backend/.env, then `docker compose -f docker-compose.prod.yml up -d api` (up -d, NOT restart). 服务器未开启应急登录，需先配置 env 并 up -d 重建容器。",
  network:
    "Could not reach the backend. This form cannot help with that — check the API container. 无法连接后端，请检查 API 容器。",
}

export function BreakGlassForm({ onSubmit }: Props) {
  const [email, setEmail] = useState("")
  const [secret, setSecret] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    const code = await onSubmit(email.trim(), secret)
    setBusy(false)
    // No branch for success: a minted session flips the auth status and the
    // page navigates away underneath this component.
    if (code) setError(MESSAGES[code] ?? MESSAGES.denied)
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-amber-700 dark:text-amber-400">
        <IconAlertTriangle className="mt-0.5 size-4 shrink-0" />
        <div className="text-xs leading-relaxed">
          <div className="font-semibold">
            Break-glass sign-in · 应急登录
          </div>
          <p className="mt-1">
            Bypasses Microsoft sign-in (and its MFA) for a named operator during
            an IdP outage. Every attempt is recorded and every success is logged
            as CRITICAL. Use the normal button unless Entra is actually down.
            仅在 Entra 故障时使用，全程留痕。
          </p>
        </div>
      </div>

      {error ? (
        <div
          role="alert"
          className="border-destructive/40 bg-destructive/10 text-destructive rounded-md border px-3 py-2 text-xs leading-relaxed"
        >
          {error}
        </div>
      ) : null}

      <div className="grid gap-2">
        <Label htmlFor="bg-email">Email</Label>
        <Input
          id="bg-email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@kohleservices.com"
        />
      </div>

      <div className="grid gap-2">
        <Label htmlFor="bg-secret">Break-glass secret</Label>
        <Input
          id="bg-secret"
          type="password"
          autoComplete="one-time-code"
          required
          value={secret}
          onChange={(e) => setSecret(e.target.value)}
        />
      </div>

      <Button type="submit" disabled={busy || !email || !secret}>
        {busy ? "Signing in…" : "Emergency sign in"}
      </Button>

      <a
        href="/login"
        className="text-muted-foreground hover:text-foreground text-center text-xs underline-offset-4 hover:underline"
      >
        Back to normal sign-in · 返回正常登录
      </a>
    </form>
  )
}
