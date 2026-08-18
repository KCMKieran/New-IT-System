import { Link, useLocation } from "react-router-dom"
import { IconLock } from "@tabler/icons-react"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useI18n } from "@/components/i18n-provider"
import { MANAGER, policyForPath, type PagePolicy } from "@/lib/modules"

/**
 * 403 page (auth P4b). The first one this app has ever had.
 *
 * Before P4b the only "you cannot go there" was `<Route path="*">` redirecting
 * to `/`, which swallows an unauthorised deep link silently: the user clicks a
 * bookmark, lands on the home page, and has no way to tell that from a broken
 * link. That is the hardest kind of bug to report, so this page exists to make
 * the refusal legible.
 *
 * Written for the person who just hit it, whose next action is to ask someone
 * for access — so it names the module they are missing and says who grants it,
 * rather than printing "Forbidden" and stopping.
 */
export default function Forbidden() {
  const { t } = useI18n()
  const { pathname } = useLocation()
  const policy: PagePolicy | undefined = policyForPath(pathname)

  // Manager-only pages need different advice: there is no module to ask for,
  // the answer is "you need the manager role" (or nothing at all, if the page
  // is one of the empty placeholders).
  const needsManager = policy === MANAGER
  const moduleLabel = policy && !needsManager ? t(`modules.${policy}`) : null

  return (
    <div className="p-8">
      <Card className="max-w-xl mx-auto">
        <CardHeader className="flex flex-row items-center gap-3 space-y-0">
          <div className="p-2 rounded-lg bg-muted">
            <IconLock className="w-5 h-5 text-muted-foreground" />
          </div>
          <CardTitle className="text-lg">{t("forbidden.title")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            {needsManager
              ? t("forbidden.managerBody")
              : moduleLabel
                ? t("forbidden.moduleBody", { module: moduleLabel })
                : t("forbidden.genericBody")}
          </p>
          <p className="text-sm text-muted-foreground">{t("forbidden.askManager")}</p>
          <p className="text-xs text-muted-foreground font-mono break-all">{pathname}</p>
          <div className="flex gap-2">
            <Button asChild size="sm">
              <Link to="/">{t("forbidden.backHome")}</Link>
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
