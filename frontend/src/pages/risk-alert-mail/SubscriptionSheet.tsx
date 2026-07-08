/**
 * Create / edit drawer for one mail subscription (OPT-0043 Tab 1).
 *
 * Four sections, per the spec:
 *   ① Alert source — module select + rule multi-select (empty = whole band)
 *   ② Trigger conditions — dynamic field/op/value rows, AND/OR, empty = all
 *   ③ Recipients — to / cc (comma-separated)
 *   ④ Send policy — realtime + cooldown | daily digest + HH:MM (HKT)
 *
 * Footer: test-send (existing subscriptions only — the endpoint needs an id)
 * and save. Legacy v1 `{"any":[...]}` condition trees are shown as a
 * read-only notice; the PUT omits `conditions` unless the user actually has
 * condition rows at save time (partial update keeps the stored tree — a mere
 * AND/OR toggle or an added-then-removed row never overwrites it).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Plus, Send, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { SimpleMultiSelect } from "@/components/ui/simple-multi-select";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { apiFetch } from "@/lib/fetch";

import { fmtHkTime, readErrorDetail } from "./helpers";
import {
  CONDITION_OPS,
  buildSubscriptionPayload,
  conditionsToRows,
  emptyFormState,
  type ConditionRowDraft,
  type SubscriptionFormState,
} from "./serialize";
import type {
  ConditionOp,
  ItemEnvelope,
  MailSource,
  SubscriptionRead,
  TestSendResult,
} from "./types";

const PILL_GROUP =
  "inline-flex items-center rounded-full bg-muted p-0.5";
const PILL_ITEM =
  "flex-1 rounded-full px-3 py-1 text-center text-sm text-muted-foreground data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow";

interface SubscriptionSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sources: MailSource[];
  /** null = create mode. */
  editing: SubscriptionRead | null;
  /** Called after a successful create / update so the table refetches. */
  onSaved: () => void;
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="text-sm font-semibold">{children}</h3>;
}

export function SubscriptionSheet({
  open,
  onOpenChange,
  sources,
  editing,
  onSaved,
}: SubscriptionSheetProps) {
  const [form, setForm] = useState<SubscriptionFormState>(emptyFormState());
  const [legacyConditions, setLegacyConditions] = useState(false);
  const [conditionsDirty, setConditionsDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testSending, setTestSending] = useState(false);

  // Read `sources` through a ref inside the init effect: a late-resolving
  // GET /alert-mail/sources changes the array reference, and having it in
  // the deps would re-run the init and wipe a form the user is already
  // typing in (drawer opened before the fetch settled).
  const sourcesRef = useRef(sources);
  sourcesRef.current = sources;

  // (Re)initialize the form when the drawer opens (or its target changes).
  useEffect(() => {
    if (!open) return;
    if (editing) {
      const { logic, rows, legacy } = conditionsToRows(editing.conditions);
      setForm({
        name: editing.name,
        module: editing.module,
        ruleIds: editing.rule_ids ?? [],
        logic,
        conditionRows: rows,
        mailTo: editing.mail_to,
        mailCc: editing.mail_cc ?? "",
        mode: editing.mode,
        cooldownMin: editing.cooldown_min,
        digestTime: editing.digest_time ?? "08:00",
        enabled: editing.enabled,
      });
      setLegacyConditions(legacy);
    } else {
      setForm(emptyFormState(sourcesRef.current[0]?.module ?? ""));
      setLegacyConditions(false);
    }
    setConditionsDirty(false);
  }, [open, editing]);

  // Create mode opened before the sources fetch resolved: backfill ONLY the
  // default module once sources arrive — never re-seed the whole form.
  useEffect(() => {
    if (!open || editing) return;
    setForm((f) => (f.module ? f : { ...f, module: sources[0]?.module ?? "" }));
  }, [open, editing, sources]);

  const source = useMemo(
    () => sources.find((s) => s.module === form.module),
    [sources, form.module],
  );
  const fields = source?.filterable_fields ?? [];
  const fieldTypeByKey = useMemo(
    () => new Map(fields.map((f) => [f.field, f.type])),
    [fields],
  );

  const patchForm = (patch: Partial<SubscriptionFormState>) =>
    setForm((f) => ({ ...f, ...patch }));

  const patchConditions = (patch: Partial<SubscriptionFormState>) => {
    setConditionsDirty(true);
    patchForm(patch);
  };

  const updateRow = (idx: number, patch: Partial<ConditionRowDraft>) => {
    patchConditions({
      conditionRows: form.conditionRows.map((r, i) => {
        if (i !== idx) return r;
        const next = { ...r, ...patch };
        // Str fields only support == — snap the op when the field changes.
        if (patch.field && fieldTypeByKey.get(patch.field) === "str") {
          next.op = "==";
        }
        return next;
      }),
    });
  };

  const handleModuleChange = (module: string) => {
    // Rules and filterable fields are module-specific — reset both.
    setConditionsDirty(true);
    setLegacyConditions(false);
    patchForm({ module, ruleIds: [], conditionRows: [] });
  };

  const handleSave = async () => {
    let payload;
    try {
      payload = buildSubscriptionPayload(form, fields);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
      return;
    }
    setSaving(true);
    try {
      let res: Response;
      if (editing) {
        // PUT is partial (exclude_unset): omit `conditions` when the stored
        // tree is legacy v1 and the user never touched the section, so the
        // dispatcher keeps evaluating the original tree verbatim. The empty-
        // rows check also covers "dirty but no actual content" edits (AND/OR
        // pill toggled, or a row added then removed): zero rows would
        // serialize to conditions=null and irrecoverably overwrite the
        // legacy tree with "mail every alert".
        const body: Record<string, unknown> = { ...payload };
        if (legacyConditions && (!conditionsDirty || form.conditionRows.length === 0)) {
          delete body.conditions;
        }
        res = await apiFetch(`/api/v1/alert-mail/subscriptions/${editing.id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } else {
        res = await apiFetch(
          "/api/v1/alert-mail/subscriptions",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          },
          // Non-idempotent create: an auto-retry after the server already
          // inserted the row would create a duplicate subscription (the PUT
          // branch above is idempotent and keeps the retrying default).
          { retries: 0 },
        );
      }
      if (!res.ok) {
        toast.error(`保存失败: ${await readErrorDetail(res)}`);
        return;
      }
      toast.success(editing ? "订阅已更新" : "订阅已创建");
      onOpenChange(false);
      onSaved();
    } catch (err) {
      toast.error(`保存失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  };

  const handleTestSend = async () => {
    if (!editing) return;
    setTestSending(true);
    try {
      const recipient = form.mailTo.trim();
      const res = await apiFetch(
        `/api/v1/alert-mail/subscriptions/${editing.id}/test-send`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(recipient ? { recipient } : {}),
        },
        { timeoutMs: 60000, retries: 0 }, // SMTP send — never auto-retry
      );
      if (!res.ok) {
        toast.error(`试发失败: ${await readErrorDetail(res)}`);
        return;
      }
      const body = (await res.json()) as ItemEnvelope<TestSendResult>;
      toast.success(
        `试发成功 → ${body.data.recipient}` +
          (body.data.used_fallback ? "（无近期匹配告警，使用样例数据）" : ""),
        { description: body.data.subject },
      );
    } catch (err) {
      toast.error(`试发失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setTestSending(false);
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full overflow-y-auto sm:max-w-xl"
      >
        <SheetHeader>
          <SheetTitle>{editing ? `编辑订阅 · ${editing.name}` : "新建订阅"}</SheetTitle>
          <SheetDescription>
            检测规则决定页面上有什么，订阅条件决定邮箱里有什么——后者永远是前者的子集。
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-6 px-4 pb-6">
          {/* Name + enabled */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <Label htmlFor="sub-name">订阅名称</Label>
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">启用</span>
                <Switch
                  checked={form.enabled}
                  onCheckedChange={(v) => patchForm({ enabled: v === true })}
                />
              </div>
            </div>
            <Input
              id="sub-name"
              value={form.name}
              onChange={(e) => patchForm({ name: e.target.value })}
              placeholder="如：大额对冲-实时"
              maxLength={100}
            />
          </div>

          {/* ① Alert source */}
          <div className="space-y-3">
            <SectionTitle>① 告警源</SectionTitle>
            <div className="space-y-1.5">
              <Label>检测模块</Label>
              <Select value={form.module} onValueChange={handleModuleChange}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="选择检测模块" />
                </SelectTrigger>
                <SelectContent>
                  {sources.map((s) => (
                    <SelectItem key={s.module} value={s.module}>
                      {s.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>检测规则（留空 = 该模块全部规则）</Label>
              <SimpleMultiSelect
                options={(source?.rules ?? []).map((r) => ({
                  value: String(r.id),
                  label: `#${r.id} ${r.name}${r.enabled ? "" : "（已停用）"}`,
                }))}
                value={form.ruleIds.map(String)}
                onValueChange={(vals) =>
                  patchForm({
                    ruleIds: vals
                      .filter((v) => v !== "__ALL__")
                      .map((v) => Number(v))
                      .filter((n) => Number.isFinite(n)),
                  })
                }
                placeholder="全部规则（默认）"
                searchPlaceholder="搜索规则..."
              />
            </div>
          </div>

          {/* ② Trigger conditions */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <SectionTitle>② 触发条件</SectionTitle>
              <ToggleGroup
                type="single"
                value={form.logic}
                onValueChange={(v) =>
                  v && patchConditions({ logic: v as "and" | "or" })
                }
                className={PILL_GROUP}
              >
                <ToggleGroupItem value="and" className={PILL_ITEM}>
                  AND
                </ToggleGroupItem>
                <ToggleGroupItem value="or" className={PILL_ITEM}>
                  OR
                </ToggleGroupItem>
              </ToggleGroup>
            </div>

            {/* Shown whenever saving would still preserve the legacy tree
                (no condition rows) — matches the handleSave guard exactly. */}
            {legacyConditions && form.conditionRows.length === 0 && (
              <p className="rounded-md border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-500/40 dark:bg-amber-950/40 dark:text-amber-300">
                该订阅使用 v1 旧版条件格式（只读）。不改动本节则保存时保持原条件；
                一旦添加新条件行，将以新格式覆盖。
              </p>
            )}

            {form.conditionRows.map((row, idx) => {
              const isStr = fieldTypeByKey.get(row.field) === "str";
              return (
                <div key={idx} className="flex items-center gap-2">
                  <Select
                    value={row.field || undefined}
                    onValueChange={(v) => updateRow(idx, { field: v })}
                  >
                    <SelectTrigger className="min-w-0 flex-1">
                      <SelectValue placeholder="字段" />
                    </SelectTrigger>
                    <SelectContent>
                      {fields.map((f) => (
                        <SelectItem key={f.field} value={f.field}>
                          {f.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Select
                    value={row.op}
                    onValueChange={(v) => updateRow(idx, { op: v as ConditionOp })}
                  >
                    <SelectTrigger className="w-20 shrink-0">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {(isStr ? ["=="] : CONDITION_OPS).map((op) => (
                        <SelectItem key={op} value={op}>
                          {op}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Input
                    className="w-28 shrink-0"
                    value={row.value}
                    inputMode={isStr ? "text" : "decimal"}
                    placeholder="值"
                    onChange={(e) => updateRow(idx, { value: e.target.value })}
                  />
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-8 w-8 shrink-0 p-0 text-destructive hover:text-destructive"
                    aria-label="删除条件"
                    onClick={() =>
                      patchConditions({
                        conditionRows: form.conditionRows.filter((_, i) => i !== idx),
                      })
                    }
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>
              );
            })}

            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                patchConditions({
                  conditionRows: [
                    ...form.conditionRows,
                    { field: fields[0]?.field ?? "", op: ">=", value: "" },
                  ],
                })
              }
            >
              <Plus className="mr-1.5 h-4 w-4" />
              添加条件
            </Button>
            <p className="text-xs text-muted-foreground">
              条件留空 = 该模块（所选规则）的全部告警都发送。
            </p>
          </div>

          {/* ③ Recipients */}
          <div className="space-y-3">
            <SectionTitle>③ 收件人</SectionTitle>
            <div className="space-y-1.5">
              <Label htmlFor="sub-to">收件人 To（逗号分隔）</Label>
              <Input
                id="sub-to"
                value={form.mailTo}
                onChange={(e) => patchForm({ mailTo: e.target.value })}
                placeholder="kieran.xiang@kohleservices.com"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="sub-cc">抄送 CC（可选，逗号分隔）</Label>
              <Input
                id="sub-cc"
                value={form.mailCc}
                onChange={(e) => patchForm({ mailCc: e.target.value })}
                placeholder=""
              />
            </div>
          </div>

          {/* ④ Send policy */}
          <div className="space-y-3">
            <SectionTitle>④ 发送策略</SectionTitle>
            <ToggleGroup
              type="single"
              value={form.mode}
              onValueChange={(v) =>
                v && patchForm({ mode: v as "realtime" | "digest" })
              }
              className={PILL_GROUP}
            >
              <ToggleGroupItem value="realtime" className={PILL_ITEM}>
                实时
              </ToggleGroupItem>
              <ToggleGroupItem value="digest" className={PILL_ITEM}>
                每日汇总
              </ToggleGroupItem>
            </ToggleGroup>

            {form.mode === "realtime" ? (
              <div className="space-y-1.5">
                <Label htmlFor="sub-cooldown">同一账户冷却（分钟，0-1440）</Label>
                <Input
                  id="sub-cooldown"
                  type="number"
                  min={0}
                  max={1440}
                  className="w-32"
                  value={form.cooldownMin}
                  onChange={(e) =>
                    patchForm({ cooldownMin: Number(e.target.value) })
                  }
                />
              </div>
            ) : (
              <div className="space-y-1.5">
                <Label htmlFor="sub-digest-time">每日发送时间（HKT）</Label>
                <Input
                  id="sub-digest-time"
                  type="time"
                  className="w-32"
                  value={form.digestTime}
                  onChange={(e) => patchForm({ digestTime: e.target.value })}
                />
              </div>
            )}
          </div>

          {editing && (
            <p className="text-xs text-muted-foreground">
              最近更新：{fmtHkTime(editing.updated_at)}
              {editing.updated_by ? ` · ${editing.updated_by}` : ""}
            </p>
          )}

          {/* Footer actions */}
          <div className="flex flex-wrap items-center gap-2 border-t pt-4 [&>button]:min-w-[112px]">
            <Button onClick={handleSave} disabled={saving}>
              {saving ? "保存中..." : "保存"}
            </Button>
            <Button
              variant="outline"
              onClick={handleTestSend}
              disabled={!editing || testSending}
              title={
                editing
                  ? "发送一封 [TEST] 邮件到当前输入的收件人"
                  : "保存订阅后才能试发"
              }
            >
              <Send className="mr-1.5 h-4 w-4" />
              {testSending ? "试发中..." : "试发"}
            </Button>
            {!editing && (
              <span className="text-xs text-muted-foreground">保存后可试发</span>
            )}
          </div>
        </div>
      </SheetContent>
    </Sheet>
  );
}
