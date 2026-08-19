import { IconMailForward, IconShieldLock } from "@tabler/icons-react"

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useAuth } from "@/providers/auth-provider"

/**
 * The landing screen for an account with no modules granted (2026-08-19).
 *
 * Until the home page became the `dashboard` module, this state could not
 * exist: `allowed_modules = []` still saw the front page, so a new joiner had
 * somewhere to land. Now `[]` — which is the JIT default every new colleague is
 * provisioned with — can open nothing at all, and without this screen their
 * first ever visit would be a 403 on `/`, i.e. "the system is broken" rather
 * than "you have not been given access yet".
 *
 * Deliberately NOT wired to `useI18n()`. Every other page picks one language
 * from the toggle in the header — but the person reading this one has just
 * logged in for the first time, has never opened that toggle, and the whole
 * message is an instruction they have to act on. Showing it in a language they
 * may not read, with the switch hidden behind a menu, is exactly the failure
 * this page exists to prevent. So both languages are on screen at once, English
 * first because the sign-in flow they just came through is in English.
 *
 * The email is shown because it is what IT needs in order to find the account —
 * Entra names and mailbox aliases differ, and a colleague reading their own
 * address off the screen cannot get it wrong.
 */
export default function NoModules() {
  const { user } = useAuth()

  return (
    <div className="p-8">
      <Card className="mx-auto max-w-xl">
        <CardHeader className="flex flex-row items-center gap-3 space-y-0">
          <div className="rounded-lg bg-muted p-2">
            <IconShieldLock className="h-5 w-5 text-muted-foreground" />
          </div>
          <CardTitle className="text-lg leading-snug">
            No access has been granted yet
            <span className="mt-0.5 block text-base font-normal text-muted-foreground">
              尚未获得任何访问权限
            </span>
          </CardTitle>
        </CardHeader>

        <CardContent className="space-y-5">
          <div className="space-y-1.5">
            <p className="text-sm">
              You are signed in, but no modules have been assigned to your account
              yet, so there is no page for you to open.
            </p>
            <p className="text-sm text-muted-foreground">
              你的账号已成功登录，但还没有被分配任何模块权限，因此暂时没有可以访问的页面。
            </p>
          </div>

          <div className="space-y-1.5 rounded-lg border bg-muted/40 p-4">
            <p className="flex items-start gap-2 text-sm font-medium">
              <IconMailForward className="mt-0.5 h-4 w-4 shrink-0" />
              Please contact the IT department and tell them which modules you
              need — Dashboard, CS Department, Data Query or Risk Control.
            </p>
            <p className="pl-6 text-sm text-muted-foreground">
              请联系 IT 部门开通权限，并说明你需要哪些模块（首页 / 客服部 / 数据查询 / 风险控制）。
            </p>
          </div>

          {user?.email ? (
            <p className="text-xs text-muted-foreground">
              Signed in as / 当前登录账号:{" "}
              <span className="font-mono">{user.email}</span>
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
