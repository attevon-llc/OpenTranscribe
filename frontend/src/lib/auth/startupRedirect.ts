/**
 * Where the first load sends the visitor once the session check has settled, or null to stay.
 * Pure: the layout supplies the path, the verdict and its route tables.
 */
export interface StartupRedirectInput {
  isAuthenticated: boolean;
  path: string;
  isCloudEdition: boolean;
  /** Paths reachable without a session. */
  publicPaths: readonly string[];
  /** Local-credential routes, dead on the cloud edition (the hosted IdP owns them). */
  localCredentialPaths: readonly string[];
}

export function startupRedirect(input: StartupRedirectInput): '/login' | '/' | null {
  const { isAuthenticated, path, isCloudEdition, publicPaths, localCredentialPaths } = input;
  // Cloud edition: the hosted IdP's sign-in component on /login handles sign-up and recovery
  // inline, so any hit on a local credential route is redirected to /login.
  if (isCloudEdition && localCredentialPaths.includes(path)) return '/login';

  const isPublicPath = publicPaths.includes(path);
  if (!isAuthenticated && !isPublicPath) return '/login';
  if (isAuthenticated && isPublicPath) return '/';
  return null;
}
