/** Small shared helpers for the Alert Mail Center page. */

/** Render a backend UTC ISO timestamp in Asia/Hong_Kong (project convention). */
export function fmtHkTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-CA", {
      timeZone: "Asia/Hong_Kong",
      hour12: false,
    });
  } catch {
    return iso;
  }
}

/** Extract a human-readable message from a FastAPI error response
 *  (`{"detail": "..."}` or a 422 Pydantic error array). */
export async function readErrorDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((d) => {
          const e = d as { loc?: unknown[]; msg?: string };
          const loc = Array.isArray(e.loc) ? e.loc.join(".") : "";
          return loc ? `${loc}: ${e.msg ?? ""}` : (e.msg ?? "");
        })
        .filter(Boolean)
        .join("; ");
    }
    return `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}
