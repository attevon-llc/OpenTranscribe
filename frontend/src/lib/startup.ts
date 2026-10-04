/**
 * Root-layout startup: the three independent boot requests run concurrently.
 *
 * `locale.initialize()` (loads the translation bundle), `getAuthMethods()` (a public
 * request that only feeds the classification banner) and `initAuth()` (the session
 * check that gates first render) used to run strictly in series, so a visitor waited
 * for the sum of three round trips. None needs another's result, so they are started
 * together and each failure is isolated: auth-methods failure only loses the banner, an
 * auth failure still sends the visitor to /login, a locale failure never blocks either.
 */
export interface StartupTasks<M> {
  initLocale: () => Promise<unknown>;
  getAuthMethods: () => Promise<M>;
  initAuth: () => Promise<unknown>;
}

export interface StartupResult<M> {
  /** Auth methods, or null when the request failed. */
  authMethods: M | null;
  /** Set when `initAuth` rejected; the caller owns the redirect. */
  authError: unknown;
  authFailed: boolean;
}

export async function runStartup<M>(tasks: StartupTasks<M>): Promise<StartupResult<M>> {
  const [locale, methods, auth] = await Promise.allSettled([
    tasks.initLocale(),
    tasks.getAuthMethods(),
    tasks.initAuth(),
  ]);

  if (locale.status === 'rejected') {
    console.warn('[Layout] Failed to initialize locale:', locale.reason);
  }
  if (methods.status === 'rejected') {
    console.warn('[Layout] Failed to fetch auth methods for banner:', methods.reason);
  }

  return {
    authMethods: methods.status === 'fulfilled' ? methods.value : null,
    authFailed: auth.status === 'rejected',
    authError: auth.status === 'rejected' ? auth.reason : null,
  };
}
