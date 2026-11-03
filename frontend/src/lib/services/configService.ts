import axiosInstance from '../axios';

type ProtectedMediaField = {
  name: string;
  label: string;
  type: string;
};

export type ProtectedMediaAuthConfig = {
  hosts: string[];
  hosts_with_stored_credentials?: string[];
  auth_type: string;
  fields: ProtectedMediaField[];
};

let protectedConfigs: ProtectedMediaAuthConfig[] = [];
let loaded = false;
let loadingPromise: Promise<void> | null = null;
// Bumped on every reset. A fetch captures the generation it started under and
// discards its own result if that generation has since moved — otherwise a
// fetch still in flight at logout resolves AFTER reset and overwrites
// protectedConfigs with the PREVIOUS user's data, reintroducing the exact
// leak resetProtectedMediaAuthConfig exists to prevent.
let generation = 0;

export async function loadProtectedMediaAuthConfig(): Promise<void> {
  if (loaded) return;
  if (loadingPromise) return loadingPromise;

  const startedAtGeneration = generation;

  loadingPromise = (async () => {
    try {
      const resp = await axiosInstance.get<ProtectedMediaAuthConfig[]>(
        '/system/config/protected-media-auth'
      );
      if (generation !== startedAtGeneration) return;
      protectedConfigs = resp.data ?? [];
      loaded = true;
    } catch (e) {
      if (generation !== startedAtGeneration) return;
      // Config is optional; swallow errors and leave configs empty
      console.error('Failed to load protected media auth config', e);
      protectedConfigs = [];
      loaded = true;
    } finally {
      if (generation === startedAtGeneration) {
        loadingPromise = null;
      }
    }
  })();

  return loadingPromise;
}

/**
 * Drop the cached config so the next session re-fetches it.
 *
 * Registered in `$lib/session/clearUserState`. `hosts_with_stored_credentials`
 * is PER-USER — it is what drives the "credentials already stored" affordance —
 * and `loaded` is a once-only latch, so without this reset User B was shown
 * which protected hosts User A had saved credentials for until a hard reload.
 *
 * Bumping `generation` also discards the result of any fetch already in
 * flight at the moment of reset — see the comment on `generation` above.
 */
export function resetProtectedMediaAuthConfig(): void {
  protectedConfigs = [];
  loaded = false;
  loadingPromise = null;
  generation++;
}

const DEFAULT_PORTS: Record<string, string> = { 'http:': '80', 'https:': '443' };

// One identity rule, mirrored by `_host_identity` in backend protected_media_plugins/mediacms.py:
// lower-cased hostname plus the port, where an absent port means the scheme's default.
function hostIdentity(rawHost: string, protocol: string): string | null {
  const host = rawHost.trim();
  // The backend compares the raw string; WHATWG URL normalises these (userinfo, backslash,
  // percent-escapes, IDN), so the UI would match a host the server refuses.
  if (/[@\\%]/.test(host) || /[^\x21-\x7e]/.test(host)) return null;
  try {
    const parsed = new URL(`${protocol}//${host}`);
    return `${parsed.hostname.toLowerCase()}:${parsed.port || DEFAULT_PORTS[protocol] || ''}`;
  } catch {
    return null;
  }
}

export function getAuthConfigForUrl(url: URL): ProtectedMediaAuthConfig | null {
  const wanted = hostIdentity(url.host, url.protocol);
  if (wanted === null) return null;
  for (const cfg of protectedConfigs) {
    if (cfg.hosts?.some((h) => hostIdentity(h, url.protocol) === wanted)) {
      return cfg;
    }
  }
  return null;
}
