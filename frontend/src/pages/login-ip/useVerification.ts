/**
 * useVerification — encapsulates the 2-step email verification flow that
 * protects watchlist write operations.
 *
 * Flow:
 *   1. `openFor(action, payload, description)` — opens the dialog
 *   2. User fills email → `requestCode()` sends the 6-digit code
 *   3. User fills code → `verifyAndRun()` calls /verify-action and triggers
 *      `onSuccess` (so the page can refresh its data after e.g. deletion)
 *
 * This is the same pattern IB Financial uses (inline in IBFinancialMonitor.tsx).
 * We extracted it here because it's reused across three Login IP watchlist
 * operations (add / update / delete). If a third module ever needs this, it's
 * worth promoting to `@/components/VerificationDialog`.
 */

import { useState } from "react";
import { toast } from "sonner";
import { apiFetch } from "@/lib/fetch";

export interface UseVerificationResult {
  open: boolean;
  setOpen: (v: boolean) => void;

  email: string;
  setEmail: (v: string) => void;

  code: string;
  setCode: (v: string) => void;

  codeSent: boolean;
  verifying: boolean;
  actionDescription: string;

  openFor: (
    action: string,
    payload: Record<string, unknown>,
    description: string,
  ) => void;
  requestCode: () => Promise<void>;
  verifyAndRun: () => Promise<void>;
}

/**
 * @param onSuccess Called after a successful verify-action; page should
 *                  re-fetch its data here.
 */
export function useVerification(onSuccess: () => void): UseVerificationResult {
  const [open, setOpen] = useState(false);
  const [action, setAction] = useState("");
  const [payload, setPayload] = useState<Record<string, unknown>>({});
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [actionDescription, setActionDescription] = useState("");

  const openFor = (
    nextAction: string,
    nextPayload: Record<string, unknown>,
    description: string,
  ) => {
    setAction(nextAction);
    setPayload(nextPayload);
    setActionDescription(description);
    setEmail("");
    setCode("");
    setCodeSent(false);
    setVerifying(false);
    setOpen(true);
  };

  const requestCode = async () => {
    if (!email) {
      toast.error("请输入验证邮箱");
      return;
    }
    try {
      const res = await apiFetch("/api/v1/login-ip/request-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, action }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      setCodeSent(true);
      toast.success(`验证码已发送到 ${email}`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const verifyAndRun = async () => {
    setVerifying(true);
    try {
      const res = await apiFetch("/api/v1/login-ip/verify-action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, code, action, payload }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      toast.success(data.message || "操作成功");
      setOpen(false);
      onSuccess();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setVerifying(false);
    }
  };

  return {
    open,
    setOpen,
    email,
    setEmail,
    code,
    setCode,
    codeSent,
    verifying,
    actionDescription,
    openFor,
    requestCode,
    verifyAndRun,
  };
}
