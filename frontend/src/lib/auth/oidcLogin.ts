import { loginWithOIDC } from '$stores/auth';

export const OIDC_START_TIMEOUT_MS = 10000;

/**
 * Start the OIDC redirect, giving up after `OIDC_START_TIMEOUT_MS` so a provider that is down
 * cannot leave an infinite spinner. A successful start redirects the browser to the IdP; the
 * returned result only matters when it failed. Rejects with `Error('Connection timeout')` on
 * timeout.
 */
export async function startOidcLogin(): Promise<{ success: boolean; message?: string }> {
  const timeout = new Promise<never>((_, reject) =>
    setTimeout(() => reject(new Error('Connection timeout')), OIDC_START_TIMEOUT_MS)
  );
  return Promise.race([loginWithOIDC(), timeout]);
}
