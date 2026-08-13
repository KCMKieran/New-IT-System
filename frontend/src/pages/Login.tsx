// src/pages/Login.tsx
import { useState } from "react"
import { Navigate, useSearchParams } from "react-router-dom"
import { useAuth } from "@/providers/auth-provider"
import { useI18n } from "@/components/i18n-provider"
import { LoginForm } from "@/components/login-form"
import BlurText from "@/components/blur-text"
import { PageLoader } from "@/components/LazyErrorBoundary"
import { errorKeyFor } from "@/lib/auth-errors"
import { sanitizeReturnTo } from "@/lib/auth-session"

export default function LoginPage() {
  const { t } = useI18n()
  const { status, loginWithMicrosoft } = useAuth()
  const [searchParams] = useSearchParams()
  // Clicking the button navigates the whole page away, so this only has to
  // survive the moment in between — but in that moment a second click would
  // start a second OIDC transaction and orphan the first.
  const [busy, setBusy] = useState(false)

  const errorKey = errorKeyFor(searchParams.get("error"))
  const returnTo = sanitizeReturnTo(searchParams.get("return_to"))

  if (status === "loading") return <PageLoader />
  // Already signed in (or the kill switch is thrown): nothing to do here.
  if (status === "authenticated") return <Navigate to={returnTo || "/"} replace />

  function handleSignIn() {
    setBusy(true)
    loginWithMicrosoft(returnTo || undefined)
  }

  // Hero image on the left; place image under /public/images/login-hero.png
  return (
    <div className="grid min-h-svh grid-cols-1 md:grid-cols-2">
      <div className="hidden md:block bg-muted">
        <img
          src="/images/login-hero.png"
          alt="Welcome"
          className="h-full w-full object-cover"
        />
      </div>
      <div className="flex items-center justify-center p-6">
        <div className="w-full max-w-sm">
          <div className="mb-6 text-center">
            {/* Animated page title */}
            <BlurText
              text="Welcome to KCM Trade Analytics System"
              delay={150}
              animateBy="words"
              direction="top"
              className="text-3xl font-bold tracking-tight justify-center"
              repeatEveryMs={5000}
            />
          </div>
          <LoginForm
            onSignIn={handleSignIn}
            errorMessage={errorKey ? t(errorKey) : null}
            busy={busy}
          />
        </div>
      </div>
    </div>
  )
}
