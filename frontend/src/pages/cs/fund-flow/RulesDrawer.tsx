/**
 * Drawer for editing the detection rule set.
 * Mirrors the pattern from risk-monitor's config drawer.
 */

import { useEffect, useState } from "react";
import {
  Drawer,
  DrawerContent,
  DrawerHeader,
  DrawerTitle,
  DrawerClose,
} from "@/components/ui/drawer";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Plus, Trash2, Save } from "lucide-react";
import { apiFetch } from "@/lib/fetch";
import type { FundFlowConfig, FundFlowRule } from "./types";
import { useI18n } from "@/components/i18n-provider";

const emptyRule = (name: string): FundFlowRule => ({
  name,
  enabled: true,
  lookback_days: 7,
  min_deposit_count: 3,
  min_withdrawal_count: 3,
  combine_logic: "OR",
  max_trade_count: 1,
  min_deposit_amount_usd: null,
  min_withdrawal_amount_usd: null,
});

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}

export function RulesDrawer({ open, onOpenChange, onSaved }: Props) {
  const [rules, setRules] = useState<FundFlowRule[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { t } = useI18n();

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    apiFetch("/api/v1/cs/fund-flow/config", { signal: controller.signal })
      .then((r) => r.json())
      .then((j: FundFlowConfig) => setRules(j.rules))
      .catch((e) => {
        if (e?.name !== "AbortError") setError(String(e));
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [open]);

  const updateRule = (idx: number, patch: Partial<FundFlowRule>) => {
    setRules((prev) => prev.map((r, i) => (i === idx ? { ...r, ...patch } : r)));
  };

  const addRule = () =>
    setRules((prev) => [...prev, emptyRule(t("fundFlowPage.rules.newRuleName"))]);
  const removeRule = (idx: number) =>
    setRules((prev) => prev.filter((_, i) => i !== idx));

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/cs/fund-flow/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rules }),
      });
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(
          t("fundFlowPage.common.saveFailed", { status: res.status, detail: txt }),
        );
      }
      onSaved();
      onOpenChange(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Drawer open={open} onOpenChange={onOpenChange} direction="right">
      <DrawerContent className="!w-[640px] !max-w-[90vw]">
        <DrawerHeader className="flex flex-row items-center justify-between border-b">
          <DrawerTitle>{t("fundFlowPage.rules.title")}</DrawerTitle>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={addRule}>
              <Plus className="h-4 w-4 mr-1" />
              {t("fundFlowPage.rules.add")}
            </Button>
            <DrawerClose asChild>
              <Button size="sm" variant="ghost">{t("fundFlowPage.rules.close")}</Button>
            </DrawerClose>
          </div>
        </DrawerHeader>

        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {loading && (
            <p className="text-sm text-muted-foreground">
              {t("fundFlowPage.common.loading")}
            </p>
          )}
          {error && (
            <p className="text-sm text-destructive">
              {t("fundFlowPage.common.error", { message: error })}
            </p>
          )}

          {!loading && rules.length === 0 && (
            <p className="text-sm text-muted-foreground">
              {t("fundFlowPage.rules.empty")}
            </p>
          )}

          {rules.map((rule, idx) => (
            <div key={idx} className="rounded-lg border p-4 space-y-3">
              <div className="flex items-center justify-between gap-2">
                <Input
                  className="font-medium max-w-sm"
                  value={rule.name}
                  onChange={(e) => updateRule(idx, { name: e.target.value })}
                  placeholder={t("fundFlowPage.rules.namePlaceholder")}
                />
                <div className="flex items-center gap-3">
                  <label className="flex items-center gap-1 text-sm">
                    <Checkbox
                      checked={rule.enabled}
                      onCheckedChange={(v) => updateRule(idx, { enabled: !!v })}
                    />
                    {t("fundFlowPage.rules.enabled")}
                  </label>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => removeRule(idx)}
                    aria-label={t("fundFlowPage.rules.delete")}
                  >
                    <Trash2 className="h-4 w-4 text-destructive" />
                  </Button>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3 text-sm">
                <div className="space-y-1">
                  <Label>{t("fundFlowPage.rules.lookbackDays")}</Label>
                  <Input
                    type="number"
                    min={1}
                    max={90}
                    value={rule.lookback_days}
                    onChange={(e) =>
                      updateRule(idx, { lookback_days: Number(e.target.value) || 7 })
                    }
                  />
                </div>
                <div className="space-y-1">
                  <Label>{t("fundFlowPage.filters.maxTradeCount")}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={rule.max_trade_count}
                    onChange={(e) =>
                      updateRule(idx, { max_trade_count: Number(e.target.value) || 0 })
                    }
                  />
                </div>

                <div className="space-y-1">
                  <Label>{t("fundFlowPage.filters.minDepositCount")}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={rule.min_deposit_count ?? ""}
                    onChange={(e) =>
                      updateRule(idx, {
                        min_deposit_count:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    placeholder={t("fundFlowPage.filters.unlimited")}
                  />
                </div>
                <div className="space-y-1">
                  <Label>{t("fundFlowPage.filters.minWithdrawCount")}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={rule.min_withdrawal_count ?? ""}
                    onChange={(e) =>
                      updateRule(idx, {
                        min_withdrawal_count:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    placeholder={t("fundFlowPage.filters.unlimited")}
                  />
                </div>

                <div className="space-y-1">
                  <Label>{t("fundFlowPage.filters.minDepositAmount")}</Label>
                  <Input
                    type="number"
                    min={0}
                    step={100}
                    value={rule.min_deposit_amount_usd ?? ""}
                    onChange={(e) =>
                      updateRule(idx, {
                        min_deposit_amount_usd:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    placeholder={t("fundFlowPage.filters.unlimited")}
                  />
                </div>
                <div className="space-y-1">
                  <Label>{t("fundFlowPage.filters.minWithdrawAmount")}</Label>
                  <Input
                    type="number"
                    min={0}
                    step={100}
                    value={rule.min_withdrawal_amount_usd ?? ""}
                    onChange={(e) =>
                      updateRule(idx, {
                        min_withdrawal_amount_usd:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    placeholder={t("fundFlowPage.filters.unlimited")}
                  />
                </div>

                <div className="space-y-1 col-span-2">
                  <Label>{t("fundFlowPage.rules.combineLogic")}</Label>
                  <Select
                    value={rule.combine_logic}
                    onValueChange={(v) =>
                      updateRule(idx, { combine_logic: v as "OR" | "AND" })
                    }
                  >
                    <SelectTrigger className="w-44">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="OR">{t("fundFlowPage.rules.combineOr")}</SelectItem>
                      <SelectItem value="AND">{t("fundFlowPage.rules.combineAnd")}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>
          ))}
        </div>

        <div className="border-t p-4 flex justify-end gap-2">
          <DrawerClose asChild>
            <Button variant="outline">{t("fundFlowPage.rules.cancel")}</Button>
          </DrawerClose>
          <Button onClick={save} disabled={saving}>
            <Save className="h-4 w-4 mr-1" />
            {saving ? t("fundFlowPage.rules.saving") : t("fundFlowPage.rules.save")}
          </Button>
        </div>
      </DrawerContent>
    </Drawer>
  );
}
