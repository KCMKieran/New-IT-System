/**
 * Trade Window Scan — shell (contract: docs/features/window-scan.md).
 *
 * Two tabs over one scan: Entry Window Scan (who ENTERED around this instant)
 * and Close Window Scan (who EXITED around it). Same conditions, same rollup,
 * same profitability rule — only the timestamp the window is measured against
 * changes, so the tab value IS the API's `scan_by` (one concept, one value).
 *
 * This file owns three things and nothing else:
 *
 *   1. the tab strip and its `?tab=` / localStorage sync (RiskMonitor pattern:
 *      a URL param always wins so deep links keep working);
 *   2. `anchor` + `symbol` — deliberately lifted ABOVE the tabs so switching
 *      basis re-runs the same investigation instead of making the user retype
 *      the instant they are looking at;
 *   3. the page title.
 *
 * Everything else lives in `window-scan/ScanBody.tsx`, one instance per tab.
 * Radix unmounts the inactive TabsContent, which is what discards the previous
 * basis's results — showing an entry-basis client list under a "Close Window
 * Scan" heading would misattribute every number on screen.
 */

import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { toHkAnchor } from "./window-scan/format";
import { ScanBody } from "./window-scan/ScanBody";
import {
  ACTIVE_TAB_KEY,
  isScanBasis,
  SCAN_BASIS_LABELS,
  SCAN_BASIS_TABS,
  type ScanBasis,
} from "./window-scan/types";

/** The tab the page opens on when neither the URL nor storage says otherwise.
 *  Kept at the v1 behaviour so existing bookmarks land where they always did. */
const DEFAULT_TAB: ScanBasis = "open";

export default function WindowScan() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const activeTab: ScanBasis = isScanBasis(tabParam) ? tabParam : DEFAULT_TAB;

  // Drop unknown ?tab= values so the address bar matches what is rendered.
  useEffect(() => {
    if (tabParam !== null && !isScanBasis(tabParam)) {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("tab");
          return next;
        },
        { replace: true },
      );
    }
  }, [tabParam, setSearchParams]);

  // Restore the last-active tab only when the URL is silent — a deep link
  // (chat, bookmark) must never be overridden by a stale local preference.
  useEffect(() => {
    if (tabParam !== null) return;
    let saved: string | null = null;
    try {
      saved = localStorage.getItem(ACTIVE_TAB_KEY);
    } catch {
      // private mode / storage disabled — stay on the default
    }
    if (!saved || !isScanBasis(saved) || saved === DEFAULT_TAB) return;
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("tab", saved);
        return next;
      },
      { replace: true },
    );
    // Mount only; afterwards onTabChange keeps storage in sync.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onTabChange = useCallback(
    (value: string) => {
      if (!isScanBasis(value)) return;
      try {
        localStorage.setItem(ACTIVE_TAB_KEY, value);
      } catch {
        // ignore
      }
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          // Default tab → omit the param entirely for a shorter, shareable URL.
          if (value === DEFAULT_TAB) next.delete("tab");
          else next.set("tab", value);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // Investigation context — never persisted (contract §6), but shared across
  // the tabs so "same instant, other basis" is one click, not a retype.
  const [anchor, setAnchor] = useState<string>(() => toHkAnchor(new Date()));
  const [symbol, setSymbol] = useState<string>("");

  return (
    <div className="flex-1 space-y-4 overflow-x-hidden p-4 md:p-6">
      <h1 className="text-lg font-semibold tracking-tight">Trade Window Scan</h1>

      <Tabs
        value={activeTab}
        onValueChange={onTabChange}
        className="w-full min-w-0 gap-4"
      >
        {/* Two tabs fit any viewport, so no horizontal scroller is needed —
            unlike RiskMonitor's seven. Capped width keeps them from stretching
            across a wide desktop, where a full-bleed 2-column strip reads as a
            segmented control for the whole page rather than a tab bar. */}
        <TabsList className="grid w-full max-w-md grid-cols-2">
          {SCAN_BASIS_TABS.map((value) => (
            <TabsTrigger
              key={value}
              value={value}
              className="px-3 text-sm whitespace-nowrap"
            >
              {SCAN_BASIS_LABELS[value]}
            </TabsTrigger>
          ))}
        </TabsList>

        {SCAN_BASIS_TABS.map((value) => (
          <TabsContent key={value} value={value} className="mt-0">
            {/* Guarded so the inactive panel holds no grid instance at all —
                two AG-Grids sharing one persisted column key would race each
                other's saved state. */}
            {activeTab === value && (
              <ScanBody
                scanBy={value}
                anchor={anchor}
                onAnchorChange={setAnchor}
                symbol={symbol}
                onSymbolChange={setSymbol}
              />
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
