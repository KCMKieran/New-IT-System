/**
 * Typed client for the OPT-0035 view-profiles API. Thin glue over apiFetch
 * (which injects X-API-Key + X-Device-ID). 409/403 are surfaced as typed errors
 * so the Settings UI can show the right message ("claimed by another device" /
 * "not authorised to force-release").
 */
import { apiFetch } from "@/lib/fetch";
import type { ViewSnapshot } from "./snapshot";

export interface ProfileDTO {
  name: string;
  state: Record<string, string>;
  owner_device: string | null;
  owner_label: string | null;
  claimed_at: string | null;
  updated_at: string | null;
}

export class ProfileConflictError extends Error {}
export class ProfileForbiddenError extends Error {}

const BASE = "/api/v1/view-profiles";

async function detail(res: Response): Promise<string> {
  try {
    const body = await res.json();
    return body?.detail ?? res.statusText;
  } catch {
    return res.statusText;
  }
}

async function unwrap(res: Response): Promise<unknown> {
  if (res.status === 409) throw new ProfileConflictError(await detail(res));
  if (res.status === 403) throw new ProfileForbiddenError(await detail(res));
  if (!res.ok) throw new Error(await detail(res));
  return res.json();
}

function postJson(url: string, body: unknown, signal?: AbortSignal): Promise<Response> {
  return apiFetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
}

const enc = encodeURIComponent;

export async function listProfiles(signal?: AbortSignal): Promise<ProfileDTO[]> {
  const body = (await unwrap(await apiFetch(BASE, { signal }))) as { data: ProfileDTO[] };
  return body.data;
}

export async function getProfile(name: string, signal?: AbortSignal): Promise<ProfileDTO | null> {
  const res = await apiFetch(`${BASE}/${enc(name)}`, { signal });
  if (res.status === 404) return null;
  const body = (await unwrap(res)) as { data: ProfileDTO };
  return body.data;
}

export async function createProfile(name: string, signal?: AbortSignal): Promise<ProfileDTO> {
  const body = (await unwrap(await postJson(BASE, { name }, signal))) as { data: ProfileDTO };
  return body.data;
}

export async function claimProfile(
  name: string,
  label?: string,
  signal?: AbortSignal,
): Promise<ProfileDTO> {
  const body = (await unwrap(
    await postJson(`${BASE}/${enc(name)}/claim`, { label }, signal),
  )) as { data: ProfileDTO };
  return body.data;
}

export async function releaseProfile(name: string, signal?: AbortSignal): Promise<void> {
  await unwrap(await postJson(`${BASE}/${enc(name)}/release`, {}, signal));
}

export async function forceReleaseProfile(name: string, signal?: AbortSignal): Promise<void> {
  await unwrap(await postJson(`${BASE}/${enc(name)}/force-release`, {}, signal));
}

async function putState(name: string, state: ViewSnapshot, keepalive: boolean): Promise<void> {
  await unwrap(
    await apiFetch(`${BASE}/${enc(name)}/state`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state }),
      // keepalive lets the request outlive page unload. apiFetch spreads `init`
      // into fetch(), so this passes straight through. Note: sendBeacon is NOT a
      // substitute here — it can't set the X-Device-ID / X-API-Key headers this
      // endpoint requires; keepalive fetch can.
      keepalive,
    }),
  );
}

export async function saveProfileState(name: string, state: ViewSnapshot): Promise<void> {
  await putState(name, state, false);
}

/**
 * Same PUT as saveProfileState but with `keepalive: true`, so the request is not
 * abandoned when the page is unloading. Wire this to the beforeunload flush.
 */
export async function saveProfileStateKeepalive(
  name: string,
  state: ViewSnapshot,
): Promise<void> {
  await putState(name, state, true);
}
