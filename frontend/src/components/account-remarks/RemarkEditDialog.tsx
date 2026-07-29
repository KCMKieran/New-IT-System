/**
 * RemarkEditDialog — the per-account remark editor (feat/account-remarks).
 *
 * A shadcn Dialog with a textarea (maxLength = REMARK_NOTE_MAX_LEN, R2 client
 * mirror), the "最后由 {author} 于 {time} 修改" provenance line, and Save / Delete
 * actions. The note renders as PLAIN TEXT only — never innerHTML (R4 is enforced
 * in the grid renderer; this dialog only ever puts the string into a controlled
 * <textarea>).
 *
 * Identity: the author written onto the remark is resolved from the claimed View
 * Profile name; if the device has neither a claimed name nor a temp name, the
 * dialog blocks Save behind a small name prompt and stashes the typed name as
 * the temp fallback (account-remarks.md §2 待兜底细节).
 *
 * Optimistic lock (R1): the dialog echoes the row's opaque `updated_at` token
 * back as `expectedUpdatedAt`. On a 409 (RemarkConflictError) it shows
 * "他人已修改，请刷新" and asks the parent to refetch — the user can then re-open
 * with the fresh token.
 *
 * This component is presentational glue: it does NOT own the remarks store. The
 * parent passes `existing` (the current row, if any) and the hook's mutators.
 *
 * Structure (feat/client-remarks): the whole editor body lives in the
 * target-agnostic `RemarkDialogCore` — it only knows a display `title` and
 * target-less onSave/onDelete closures, so any remark anchor (account-level
 * (server, login) here, client-level user_id in
 * components/client-remarks/ClientRemarkEditDialog.tsx) can drive it. The
 * exported `RemarkEditDialog` keeps its ORIGINAL (server, login) props and just
 * binds them into the core — RiskMonitor call sites are untouched and
 * behavior-identical.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  type AccountRemark,
  REMARK_NOTE_MAX_LEN,
  RemarkConflictError,
  remarkUpdatedAtRaw,
} from "@/lib/account-remarks/api";
import {
  resolveAuthorName,
  setTempAuthorName,
} from "@/lib/account-remarks/author";

/**
 * Minimal shape the core needs from a live remark row — satisfied by both
 * AccountRemark and ClientRemark (the anchor fields stay in the wrappers).
 */
export interface RemarkLike {
  note: string;
  author: string;
  /** Opaque optimistic-lock token `YYYY-MM-DDTHH:MM:SSZ#<n>` (echoed verbatim). */
  updated_at: string;
}

export interface RemarkDialogCoreProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Dialog heading, e.g. `账户备注 · MT5 / 123` or `客户备注 · 8522845`. */
  title: string;
  /** Current live row, or undefined if no remark exists yet. */
  existing: RemarkLike | undefined;
  /**
   * Save (PUT) with the target already bound by the wrapper. Resolves on
   * success; rejects RemarkConflictError on 409.
   */
  onSave: (
    note: string,
    author: string,
    expectedUpdatedAt: string | null,
  ) => Promise<unknown>;
  /** Delete (DELETE) with the target bound. `author` is for the audit row. */
  onDelete: (author: string) => Promise<void>;
  /** Re-pull the full map (called after a 409 so tokens refresh). */
  onRefetch: () => Promise<void>;
  /** Render the provenance timestamp (parent supplies its time formatter). */
  formatTime?: (raw: string | null) => string;
}

export function RemarkDialogCore({
  open,
  onOpenChange,
  title,
  existing,
  onSave,
  onDelete,
  onRefetch,
  formatTime,
}: RemarkDialogCoreProps) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  // Name prompt (degraded identity path): null = resolved, string = pending input.
  const [pendingName, setPendingName] = useState<string | null>(null);
  // F3: on a 409 we keep the user's draft and surface the other person's current
  // note here as a warning/diff (instead of silently overwriting the draft).
  const [conflictNote, setConflictNote] = useState<string | null>(null);

  // F3: only seed the draft from `existing` on the OPEN transition. `existing`'s
  // identity also changes on every refetch (incl. the 409 path, which calls
  // onRefetch) — keying the reset on `existing` would clobber the user's typed
  // draft. Read the initial value via a ref so this effect depends on `open` only.
  const existingRef = useRef(existing);
  existingRef.current = existing;
  const prevOpenRef = useRef(false);
  useEffect(() => {
    if (open && !prevOpenRef.current) {
      // Open transition: seed from the current row.
      setNote(existingRef.current?.note ?? "");
      setBusy(false);
      setPendingName(resolveAuthorName() === null ? "" : null);
      setConflictNote(null);
    }
    prevOpenRef.current = open;
  }, [open]);

  const provenance = useMemo(() => {
    if (!existing) return null;
    const when = remarkUpdatedAtRaw(existing.updated_at);
    const time = formatTime ? formatTime(when) : (when ?? "");
    return `最后由 ${existing.author || "—"} 于 ${time} 修改`;
  }, [existing, formatTime]);

  const handleSave = async () => {
    // Resolve / capture the author identity first (account-remarks.md §2).
    let author = resolveAuthorName();
    if (author === null) {
      const typed = (pendingName ?? "").trim();
      if (!typed) {
        toast.error("请先认领视图档案，或临时填写名字");
        return;
      }
      setTempAuthorName(typed);
      author = typed;
    }

    setBusy(true);
    try {
      // Read the freshest token via the ref so a retry after a 409 (which
      // refetched `existing`) carries the updated optimistic-lock token.
      await onSave(note, author, existingRef.current?.updated_at ?? null);
      toast.success("备注已保存");
      onOpenChange(false);
    } catch (err) {
      if (err instanceof RemarkConflictError) {
        // R1 / F3: someone else edited it. Keep the user's typed draft UNTOUCHED;
        // refetch only to refresh the optimistic-lock token (so the next retry
        // can succeed) and surface the other person's current note as a warning
        // rather than silently replacing the draft.
        toast.error("他人已修改，已保留你的草稿——请对比后重试");
        await onRefetch();
        setConflictNote(existingRef.current?.note ?? "（对方已删除该备注）");
      } else {
        toast.error(err instanceof Error ? err.message : "保存失败");
      }
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    // F6: the note is shared / all-staff-visible — confirm before destroying it.
    if (
      !window.confirm(
        "删除后所有同事都将看不到该备注（仅保留审计记录）。确认删除？",
      )
    ) {
      return;
    }
    // F6: resolve the author display name for the delete audit row.
    const author = resolveAuthorName() ?? (pendingName ?? "").trim();
    setBusy(true);
    try {
      await onDelete(author);
      toast.success("备注已删除");
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "删除失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {provenance ? (
            <DialogDescription>{provenance}</DialogDescription>
          ) : (
            <DialogDescription>
              暂无备注，填写后所有同事可见。
            </DialogDescription>
          )}
        </DialogHeader>

        {pendingName !== null && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="remark-author">
              你的名字（未认领视图档案，临时记录作者）
            </Label>
            <Input
              id="remark-author"
              value={pendingName}
              maxLength={120}
              placeholder="例如：Kieran"
              onChange={(e) => setPendingName(e.target.value)}
              disabled={busy}
            />
          </div>
        )}

        {conflictNote !== null && (
          <div className="border-destructive/50 bg-destructive/10 text-destructive flex flex-col gap-1 rounded-md border px-3 py-2 text-xs">
            <span className="font-medium">
              他人已修改此备注（你的草稿已保留）：
            </span>
            {/* Plain text only (R4) — controlled string, never innerHTML. */}
            <span className="text-foreground whitespace-pre-wrap">
              {conflictNote || "（空）"}
            </span>
          </div>
        )}

        <div className="flex flex-col gap-1.5">
          <Textarea
            value={note}
            maxLength={REMARK_NOTE_MAX_LEN}
            placeholder="…"
            className="min-h-32"
            onChange={(e) => setNote(e.target.value)}
            disabled={busy}
          />
          <div className="text-muted-foreground text-right text-xs">
            {note.length} / {REMARK_NOTE_MAX_LEN}
          </div>
        </div>

        <DialogFooter className="sm:justify-between">
          <Button
            variant="destructive"
            onClick={handleDelete}
            disabled={busy || !existing}
          >
            删除
          </Button>
          <Button
            onClick={handleSave}
            disabled={busy || note.trim().length === 0}
          >
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export interface RemarkEditDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Account this remark is anchored to. */
  server: string;
  login: number;
  /** Current live row, or undefined if no remark exists yet. */
  existing: AccountRemark | undefined;
  /** Save (PUT). Resolves to the new live row; rejects RemarkConflictError on 409. */
  onSave: (
    server: string,
    login: number,
    note: string,
    author: string,
    expectedUpdatedAt: string | null,
  ) => Promise<AccountRemark>;
  /** Delete (DELETE). `author` is the current display name for the audit row. */
  onDelete: (server: string, login: number, author: string) => Promise<void>;
  /** Re-pull the full map (called after a 409 so tokens refresh). */
  onRefetch: () => Promise<void>;
  /** Render the provenance timestamp (parent supplies its MT-time formatter). */
  formatTime?: (raw: string | null) => string;
}

/**
 * Account-level wrapper — the ORIGINAL public component. Props and behavior are
 * unchanged; it binds (server, login) into the target-agnostic core.
 */
export function RemarkEditDialog({
  open,
  onOpenChange,
  server,
  login,
  existing,
  onSave,
  onDelete,
  onRefetch,
  formatTime,
}: RemarkEditDialogProps) {
  return (
    <RemarkDialogCore
      open={open}
      onOpenChange={onOpenChange}
      title={`账户备注 · ${server} / ${login}`}
      existing={existing}
      onSave={(note, author, expectedUpdatedAt) =>
        onSave(server, login, note, author, expectedUpdatedAt)
      }
      onDelete={(author) => onDelete(server, login, author)}
      onRefetch={onRefetch}
      formatTime={formatTime}
    />
  );
}
