/**
 * ClientRemarkEditDialog — thin client-level (user_id) wrapper around the
 * target-agnostic RemarkDialogCore (see
 * components/account-remarks/RemarkEditDialog.tsx). All editor behavior —
 * R2 length cap, author identity resolution (claimed View Profile name →
 * localStorage temp name → prompt), the R1 409 conflict flow with
 * draft-preserving refetch, and the delete confirmation — lives in the core;
 * this wrapper only binds the user_id target and the dialog title.
 */
import {
  RemarkDialogCore,
} from "@/components/account-remarks/RemarkEditDialog";
import type { ClientRemark } from "@/lib/client-remarks/api";

export interface ClientRemarkEditDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Client (CRM user id) this remark is anchored to. */
  userId: number;
  /** Current live row, or undefined if no remark exists yet. */
  existing: ClientRemark | undefined;
  /** Save (PUT). Resolves to the new live row; rejects RemarkConflictError on 409. */
  onSave: (
    userId: number,
    note: string,
    author: string,
    expectedUpdatedAt: string | null,
  ) => Promise<ClientRemark>;
  /** Delete (DELETE). `author` is the current display name for the audit row. */
  onDelete: (userId: number, author: string) => Promise<void>;
  /** Re-pull the full map (called after a 409 so tokens refresh). */
  onRefetch: () => Promise<void>;
  /** Render the provenance timestamp (parent supplies its time formatter). */
  formatTime?: (raw: string | null) => string;
}

export function ClientRemarkEditDialog({
  open,
  onOpenChange,
  userId,
  existing,
  onSave,
  onDelete,
  onRefetch,
  formatTime,
}: ClientRemarkEditDialogProps) {
  return (
    <RemarkDialogCore
      open={open}
      onOpenChange={onOpenChange}
      title={`客户备注 · ${userId}`}
      existing={existing}
      onSave={(note, author, expectedUpdatedAt) =>
        onSave(userId, note, author, expectedUpdatedAt)
      }
      onDelete={(author) => onDelete(userId, author)}
      onRefetch={onRefetch}
      formatTime={formatTime}
    />
  );
}
