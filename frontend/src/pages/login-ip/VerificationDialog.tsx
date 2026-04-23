/**
 * VerificationDialog — presentational wrapper around the useVerification hook.
 *
 * Visually identical to the dialog inlined in IBFinancialMonitor.tsx:
 * email input (or button group if whitelist loaded), "get code" button,
 * 6-digit code input, "confirm" button. We keep the markup here so every
 * Tab that protects a write op can mount <VerificationDialog {...hook} />
 * without copy-pasting 60 lines of JSX.
 */

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { IconShieldCheck } from "@tabler/icons-react";
import type { UseVerificationResult } from "./useVerification";

export function VerificationDialog({ hook }: { hook: UseVerificationResult }) {
  return (
    <Dialog open={hook.open} onOpenChange={hook.setOpen}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>邮箱验证</DialogTitle>
          <DialogDescription>
            操作「{hook.actionDescription}」需要邮箱验证。请输入白名单邮箱并填写 6 位验证码。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1">
            <Label htmlFor="verify-email">验证邮箱</Label>
            <Input
              id="verify-email"
              placeholder="you@kohleservices.com"
              value={hook.email}
              onChange={(e) => hook.setEmail(e.target.value)}
              disabled={hook.codeSent}
            />
          </div>

          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={hook.requestCode}
              disabled={!hook.email || hook.codeSent}
            >
              {hook.codeSent ? "验证码已发送" : "获取验证码"}
            </Button>
          </div>

          {hook.codeSent && (
            <div className="space-y-1">
              <Label htmlFor="verify-code">验证码 (6 位)</Label>
              <Input
                id="verify-code"
                placeholder="000000"
                maxLength={6}
                value={hook.code}
                onChange={(e) => hook.setCode(e.target.value.replace(/\D/g, ""))}
                className="w-36 font-mono text-lg tracking-widest"
              />
            </div>
          )}

          <Button
            onClick={hook.verifyAndRun}
            disabled={!hook.codeSent || hook.code.length !== 6 || hook.verifying}
            className="w-full"
          >
            <IconShieldCheck className="mr-1.5 h-4 w-4" />
            {hook.verifying ? "验证中..." : "确认执行"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
