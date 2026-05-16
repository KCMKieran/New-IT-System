/**
 * OPT-0013: SSE subscription to /api/v1/risk-monitor/alerts/stream.
 *
 * The backend pushes one lightweight notification per scan tick when
 * SSE_ENABLED=true (otherwise the endpoint returns 503 and this hook
 * silently falls back to "unavailable" status — existing per-tab
 * setInterval polling keeps working).
 *
 * Design choices:
 *  - Native EventSource (no extra dep). Auto-reconnects with built-in
 *    exponential backoff handled by the browser.
 *  - Captures the latest event into state so consumers can re-render +
 *    decide to fire an incremental fetch.
 *  - `enabled` toggle so the hook can be turned off (env-gated or by
 *    user preference) without dismounting the consumer.
 *  - 503 short-circuit: if the server says SSE is disabled we don't
 *    let the browser thrash reconnecting — set status='unavailable' and
 *    keep the EventSource closed.
 */

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/fetch";

// EventSource cannot set custom headers, so when VITE_API_KEY is set we
// fall back to passing the key as ?api_key=. The backend middleware
// accepts the query param exclusively on /alerts/stream.
const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;
const STREAM_URL = "/api/v1/risk-monitor/alerts/stream";

function streamUrlWithKey(): string {
  if (!API_KEY) return STREAM_URL;
  const sep = STREAM_URL.includes("?") ? "&" : "?";
  return `${STREAM_URL}${sep}api_key=${encodeURIComponent(API_KEY)}`;
}

export type StreamStatus =
  | "idle"           // hook just mounted, no attempt yet
  | "connecting"     // EventSource constructed, awaiting onopen
  | "connected"      // onopen fired, receiving messages or pings
  | "reconnecting"   // onerror fired, browser will retry shortly
  | "unavailable"    // server returned 503 (SSE_ENABLED=false) — stop trying
  | "disabled";      // hook called with enabled=false

export interface ScanStreamEvent {
  type: "scan";
  tier: "all" | "fast_burst" | "slow";
  scanned_at: string;
  new_alert_count: number;
  rule_ids: number[];
  /** Local timestamp the event was received in the browser (for UI). */
  received_at: number;
}

export interface UseRiskMonitorStreamResult {
  status: StreamStatus;
  lastEvent: ScanStreamEvent | null;
  /** Total events received since mount — handy for testing the indicator. */
  eventCount: number;
}

/**
 * Subscribe to the SSE stream for the lifetime of the component.
 *
 * Usage:
 *   const { status, lastEvent } = useRiskMonitorStream();
 *   useEffect(() => {
 *     if (lastEvent) refetchAlerts();  // opt-in incremental refresh
 *   }, [lastEvent?.received_at]);
 */
export function useRiskMonitorStream(
  enabled: boolean = true,
): UseRiskMonitorStreamResult {
  const [status, setStatus] = useState<StreamStatus>(enabled ? "idle" : "disabled");
  const [lastEvent, setLastEvent] = useState<ScanStreamEvent | null>(null);
  const [eventCount, setEventCount] = useState(0);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!enabled) {
      setStatus("disabled");
      return;
    }

    let cancelled = false;
    // Probe with apiFetch first (auto-injects X-API-Key when configured)
    // so we can distinguish "SSE off" (503) from "auth issue" (403) from
    // "network down" — EventSource collapses all of these into a generic
    // onerror, producing noisy reconnect attempts when the feature is off.
    (async () => {
      try {
        const probe = await apiFetch(
          STREAM_URL,
          {
            method: "GET",
            headers: { Accept: "text/event-stream" },
            signal: AbortSignal.timeout(2000),
          },
          { timeoutMs: 2000, retries: 0 },
        );
        if (cancelled) return;
        if (probe.status === 503) {
          setStatus("unavailable");
          return;
        }
        if (probe.status === 403 || probe.status === 401) {
          // Wrong / missing key — treat as unavailable so we don't thrash
          // the EventSource. Visible in the indicator tooltip via the
          // "unavailable" message.
          setStatus("unavailable");
          return;
        }
        // Probe might have already started streaming; close it cleanly so
        // we can re-open via EventSource (which handles parsing for us).
        try {
          probe.body?.cancel();
        } catch {
          /* noop */
        }
      } catch {
        // Network error during probe — fall through to EventSource which
        // will set 'reconnecting' on the same failure.
        if (cancelled) return;
      }

      if (cancelled) return;
      setStatus("connecting");
      // streamUrlWithKey() adds ?api_key=... so the middleware passes us
      // through. EventSource can't set headers — the query param is the
      // only auth channel we have for SSE.
      const es = new EventSource(streamUrlWithKey());
      esRef.current = es;

      es.onopen = () => {
        if (!cancelled) setStatus("connected");
      };

      es.onmessage = (msg) => {
        if (cancelled) return;
        try {
          const parsed = JSON.parse(msg.data) as Omit<ScanStreamEvent, "received_at">;
          setLastEvent({ ...parsed, received_at: Date.now() });
          setEventCount((n) => n + 1);
        } catch {
          // Backend uses ": ping" / ": connected" comments for keepalive;
          // those don't fire onmessage (EventSource skips comment frames),
          // so anything that lands here that fails to parse is a real bug
          // worth surfacing in the console.
          // eslint-disable-next-line no-console
          console.warn("[useRiskMonitorStream] non-JSON event:", msg.data);
        }
      };

      es.onerror = () => {
        if (cancelled) return;
        // EventSource will auto-retry; reflect that in UI.
        setStatus("reconnecting");
      };
    })();

    return () => {
      cancelled = true;
      esRef.current?.close();
      esRef.current = null;
    };
  }, [enabled]);

  return { status, lastEvent, eventCount };
}
