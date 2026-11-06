/**
 * Session cache of the protected-media login forms (`GET /system/config/protected-media-auth`).
 *
 * The upload dialog uses it to decide, for a pasted URL, whether to offer username and
 * password fields for an authenticated media site. The answer is per user, so the cache
 * is dropped on logout and on a user switch.
 */
import axiosInstance from '$lib/axios';
import { hostIdentity } from '$lib/utils/mediaHost';

export type ProtectedMediaAuthConfig = {
  hosts: string[];
  hosts_with_stored_credentials?: string[];
  auth_type: string;
  fields: { name: string; label: string; type: string }[];
};

let configs: ProtectedMediaAuthConfig[] = [];
let loaded = false;
let pending: Promise<void> | null = null;
// Bumped by every reset, so a response requested before a logout is thrown away.
let generation = 0;

/**
 * Fetch the configs once per session. Concurrent callers share one request, and a failed
 * fetch counts as "no protected sites" for the rest of the session rather than retrying.
 */
export function loadProtectedMediaAuthConfig(): Promise<void> {
  if (loaded) return Promise.resolve();
  if (pending) return pending;

  const requestedIn = generation;
  const request = axiosInstance
    .get('/system/config/protected-media-auth')
    .then((resp) => {
      if (requestedIn !== generation) return;
      configs = resp.data ?? [];
      loaded = true;
    })
    .catch((error) => {
      if (requestedIn !== generation) return;
      console.error('Could not load the protected media login settings:', error);
      configs = [];
      loaded = true;
    })
    .finally(() => {
      if (requestedIn === generation) pending = null;
    });
  pending = request;
  return request;
}

/** Forget the cached configs (logout or user switch) and discard any fetch still in flight. */
export function resetProtectedMediaAuthConfig(): void {
  configs = [];
  loaded = false;
  pending = null;
  generation += 1;
}

/**
 * The config whose hosts include the URL's host, compared with the same identity rule as
 * the backend (hostname case-insensitive, absent port = the scheme's default port).
 * Only the host counts; path, query and userinfo are ignored. Null when nothing matches.
 */
export function getAuthConfigForUrl(url: URL): ProtectedMediaAuthConfig | null {
  const wanted = hostIdentity(url.host, url.protocol);
  if (wanted === null) return null;
  return (
    configs.find((config) =>
      (config.hosts ?? []).some((host) => hostIdentity(host, url.protocol) === wanted)
    ) ?? null
  );
}
