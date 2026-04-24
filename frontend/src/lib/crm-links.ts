/**
 * KCM CRM deep links (same rules as RiskMonitor / ClientPnL* pages).
 * Fresh-grad note: `server` here is the JSON label (MT4_Live / MT5 / MT4_Live2);
 * sid prefixes align with `backend/app/core/sql_helpers` SID_MAP.
 */
const CRM_BASE = "https://mt4.kohleglobal.com";

export function crmUserUrl(
  clientId: string | number | null | undefined,
): string | null {
  if (clientId === null || clientId === undefined || clientId === "")
    return null;
  return `${CRM_BASE}/crm/users/${String(clientId).trim()}`;
}

/**
 * MT account page: /crm/accounts/{sid}-{login}
 */
export function crmAccountUrl(
  server: string | null | undefined,
  login: string | number | null | undefined,
): string | null {
  if (login === null || login === undefined || login === "") return null;
  const loginStr = String(login).trim();
  if (!loginStr) return null;
  const s = (server || "").trim();
  if (s === "MT5") return `${CRM_BASE}/crm/accounts/5-${loginStr}`;
  if (s === "MT4_Live2") return `${CRM_BASE}/crm/accounts/6-${loginStr}`;
  if (s === "MT4_Live" || s === "MT4")
    return `${CRM_BASE}/crm/accounts/1-${loginStr}`;
  return null;
}
