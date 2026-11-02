import { loginWithExternalAuth } from '$stores/auth';

export interface ExternalSignInHooks {
  /** The node the hosted sign-in component mounts into (read when mounting). */
  getNode: () => HTMLElement | null;
  /** True once the page is already signing in, so a late auth event is ignored. */
  isSignedIn: () => boolean;
  /** The hosted component is up (or setup gave up): stop the loading indicator. */
  onSettled: () => void;
  /** A session was hydrated into the local store. */
  onSuccess: () => void;
  /** Sign-in failed; `message` is the server's, if any (page falls back to its own text). */
  onFailure: (message?: string) => void;
}

/**
 * Cloud edition sign-in: load the hosted auth SDK, mount its sign-in component, and hydrate the
 * local user store once the IdP reports a session. A no-op in the community build (callers gate
 * on `isCloudEdition`; `$lib/cloud` is an inert stub there).
 */
export function createExternalSignIn(hooks: ExternalSignInHooks) {
  let unmount: (() => void) | null = null;
  let unlisten: (() => void) | null = null;

  // Hydrate the local user store from /auth/me after the IdP reports a session.
  async function completeLogin() {
    const result = await loginWithExternalAuth();
    if (result.success) hooks.onSuccess();
    else hooks.onFailure(result.message);
  }

  return {
    async setup(): Promise<void> {
      try {
        const { loadExternalAuth, mountSignIn, hasExternalSession, onAuthChange } = await import(
          '$lib/cloud'
        );
        const authHandle = await loadExternalAuth();
        if (!authHandle) {
          hooks.onSettled();
          hooks.onFailure();
          return;
        }

        // Already signed in (e.g. returning user) — hydrate and go.
        if (await hasExternalSession()) {
          await completeLogin();
          return;
        }

        // React to sign-in completion (the IdP handles the credential + MFA flow).
        unlisten = await onAuthChange(() => {
          void (async () => {
            if (!hooks.isSignedIn() && (await hasExternalSession())) {
              await completeLogin();
            }
          })();
        });

        const node = hooks.getNode();
        if (node) {
          // afterSignInUrl/afterSignUpUrl keep the user in-app; the listener does
          // the store hydration + navigation.
          unmount = await mountSignIn(node, {
            afterSignInUrl: '/',
            afterSignUpUrl: '/',
            signUpUrl: '/register',
          });
        }
        hooks.onSettled();
      } catch (err) {
        console.error('External sign-in setup failed:', err);
        hooks.onSettled();
        hooks.onFailure();
      }
    },
    /** Tear down the hosted component and listener. */
    destroy(): void {
      if (unmount) unmount();
      if (unlisten) unlisten();
    },
  };
}
