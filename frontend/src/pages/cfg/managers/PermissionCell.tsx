/**
 * The 权限 column: read-only badges for what is granted, plus a small edit
 * button that opens the module picker in a Popover.
 *
 * The whole point of this cell is to make `["*"]` and `[]` look different on
 * screen, because they are the two ends of the privilege range and they are one
 * click apart:
 *
 *   ["*"] → every module, INCLUDING modules that do not exist yet
 *   []    → nothing at all: settings, search and view profiles, no business
 *           page — not even the home page, which became the grantable
 *           `dashboard` module on 2026-08-19
 *
 * So they get two badges that share no colour, no icon and no wording, and `[]`
 * deliberately does NOT render as the "—" this page uses for missing data — an
 * empty grant is a decision someone made, not an absent value.
 *
 * ⚠ Both ends are ordinary arrays since 2026-08-27; `["*"]` used to be SQL
 * NULL, i.e. the ABSENCE of a value, which is exactly why this cell had to
 * work so hard to keep it from rendering as "missing".
 *
 * Ticking every box is a third, distinct state: it lists the keys that exist
 * today, so when an `ai` module ships the `["*"]` users get it automatically
 * and the fully-ticked users do not. That is intentional (design §4.3.3) — and
 * it is not hypothetical: `dashboard` shipped that way on 2026-08-19, which is
 * why the rollout had to backfill it into every explicit row by hand.
 *
 * Managers bypass the module gate at request time, so a manager row displays
 * full access. It is a DISPLAY rule only — the stored array is never rewritten
 * on their behalf, because it is exactly what comes back into force the day
 * they are demoted. The picker therefore edits (and shows) the stored value.
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import {
  IconBan,
  IconPencil,
  IconShieldCheck,
  IconWorld,
} from "@tabler/icons-react";
import { cn } from "@/lib/utils";
import { ALL_MODULES } from "@/lib/modules";
import type { Module } from "./types";

type Props = {
  value: string[];
  modules: Module[];
  /** True while a PATCH for this row is in flight, or when the row is read-only. */
  disabled?: boolean;
  /** Managers bypass the module gate entirely; the badges say so. Storage is
   *  untouched — see the file header. */
  isManagerRow?: boolean;
  onChange: (next: string[]) => void;
};

export function PermissionCell({
  value,
  modules,
  disabled,
  isManagerRow,
  onChange,
}: Props) {
  const [open, setOpen] = useState(false);

  const all = value.includes(ALL_MODULES);
  // The sentinel is stripped from the per-module view: it is the switch above
  // the boxes, not a box, and leaving it in `granted` would render it as a
  // badge labelled "*" next to the "All modules" badge that already says it.
  const granted = value.filter((k) => k !== ALL_MODULES);
  // Widened to string[] on purpose: a grant the catalogue does not know about
  // (an `ai` key added server-side before this bundle shipped) still has to be
  // comparable here instead of being narrowed out of existence.
  const knownKeys: string[] = modules.map((m) => m.key);
  const unknownGranted = granted.filter((k) => !knownKeys.includes(k));
  const allKnownTicked =
    knownKeys.length > 0 && knownKeys.every((k) => granted.includes(k));

  const labelFor = (key: string) =>
    modules.find((m) => m.key === key)?.label_en ?? key;

  /**
   * Single write policy for every checkbox in this popover, per-module and
   * Select-all alike: catalogue keys first in catalogue order — so the stored
   * array is stable across edits and two users with the same grants compare
   * equal in the DB — then any granted key this browser's catalogue does not
   * recognise, carried through untouched.
   *
   * Carrying them matters because the catalogue can be stale: if the
   * `/admin/modules` request failed we are editing against FALLBACK_MODULES,
   * and dropping someone's `ai` grant just because this tab never learned that
   * `ai` exists would be a permission change nobody asked for. Select-all and
   * clear-all obey this too — a bulk control is exactly where that silent loss
   * would be easiest to miss.
   *
   * Defensive today, not live: the backend 422s on any key outside its own
   * MODULE_KEYS, so an unrecognised key cannot currently be stored and
   * `unknownGranted` is always empty in practice. It becomes reachable the
   * moment the server ships a module before this bundle does.
   */
  const write = (nextKnown: string[]) =>
    onChange([
      ...knownKeys.filter((k) => nextKnown.includes(k)),
      ...unknownGranted,
    ]);

  const toggleModule = (key: string, checked: boolean) =>
    write(checked ? [...granted, key] : granted.filter((k) => k !== key));

  return (
    <div className="flex items-start justify-between gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-1">
        {isManagerRow ? (
          <Badge className="border-indigo-500/40 bg-indigo-500/15 text-indigo-700 dark:text-indigo-300">
            <IconShieldCheck />
            All modules (manager)
          </Badge>
        ) : all ? (
          <Badge>
            <IconWorld />
            All modules
          </Badge>
        ) : granted.length === 0 ? (
          <Badge
            variant="outline"
            className="border-amber-500/60 text-amber-600 dark:text-amber-400"
          >
            <IconBan />
            No modules
          </Badge>
        ) : (
          granted.map((key) => (
            <Badge key={key} variant="secondary">
              {labelFor(key)}
            </Badge>
          ))
        )}
      </div>

      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            className="h-8 w-8 shrink-0 p-0"
            disabled={disabled}
            aria-label="编辑权限"
          >
            <IconPencil className="h-4 w-4" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-72 p-0">
          {/* A manager is waved through require_module() by role alone, so
              their stored allowed_modules is inert until the day they are
              demoted. Editing it here would look like it took effect and would
              not, so the picker is replaced by a statement of the actual rule.
              The stored value is deliberately left untouched — it is what they
              get back on demotion. */}
          {isManagerRow ? (
            <p className="px-3 py-3 text-xs text-muted-foreground">
              manager 默认显示全部页面
            </p>
          ) : (
            <>
              <div className="flex items-center justify-between gap-3 border-b px-3 py-2.5">
                <span className="text-xs font-semibold">All modules</span>
                <Switch
                  checked={all}
                  disabled={disabled}
                  onCheckedChange={(checked) =>
                    // Leaving this state writes [] — an explicitly empty grant, not
                    // a guess at what the manager meant. They then tick what they
                    // want. Writing the five known keys here instead would quietly
                    // hand out whatever ships next.
                    // ⚠ Entering it writes ["*"] ALONE: the sentinel already
                    // contains every key, and the backend 422s a mixed array so
                    // there is exactly one spelling of "everything".
                    onChange(checked ? [ALL_MODULES] : [])
                  }
                  aria-label="全部模块（含将来新增）"
                />
              </div>

              {/* While `["*"]` is in force the individual boxes are shown ticked but
              inert: everything IS granted, and the switch above is the control
              that says so. They are not the stored value and must not be
              mistaken for it. */}
              <div
                className={cn(
                  "space-y-2.5 p-3",
                  all && "pointer-events-none opacity-50",
                )}
              >
                <label className="flex cursor-pointer items-center gap-2 text-xs font-medium">
                  <Checkbox
                    checked={all || allKnownTicked}
                    disabled={disabled || all}
                    onCheckedChange={(v) => write(v === true ? knownKeys : [])}
                  />
                  <span>Select all</span>
                </label>

                <div className="h-px bg-border" />

                {modules.map((m) => (
                  <label
                    key={m.key}
                    className="flex cursor-pointer items-center gap-2 text-xs"
                  >
                    <Checkbox
                      checked={all || granted.includes(m.key)}
                      disabled={disabled || all}
                      onCheckedChange={(v) => toggleModule(m.key, v === true)}
                    />
                    <span>{m.label_en}</span>
                    <span className="ml-auto text-muted-foreground">
                      {m.label_zh}
                    </span>
                  </label>
                ))}
              </div>
            </>
          )}
        </PopoverContent>
      </Popover>
    </div>
  );
}
