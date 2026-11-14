import { get, writable } from 'svelte/store';
import { acknowledgeBanner, logout, type AuthMethods } from '$stores/auth';
import { toastStore } from '$stores/toast';
import { t } from '$stores/locale';

export type BannerClassification =
  | 'UNCLASSIFIED'
  | 'CUI'
  | 'FOUO'
  | 'CONFIDENTIAL'
  | 'SECRET'
  | 'TOP SECRET'
  | 'TOP SECRET//SCI';

/** What the page should do after `acknowledge`. */
export type AcknowledgeOutcome = 'signed-in' | 'dismissed' | 'failed';

/**
 * State and flows of the login banner (FedRAMP AC-8).
 *
 * Consent is a SERVER record — `POST /auth/banner/acknowledge` — because
 * `get_current_active_user` refuses every non-exempt route with
 * `detail.code === "banner_acknowledgment_required"` until that timestamp exists and post-dates
 * the current banner wording. The old `sessionStorage` flag never reached the server, so a
 * deployment with the banner enabled was unusable after login.
 *
 * `consentPending` is in-memory ONLY, by design: it gates nothing but the login page's own
 * modal, and persisting it is what made the fake look real.
 */
export function createLoginBanner() {
  const enabled = writable(false);
  const classification = writable<BannerClassification>('UNCLASSIFIED');
  const consentPending = writable(false);
  const ackPending = writable(false);
  const ackError = writable('');

  return {
    enabled,
    classification,
    consentPending,
    ackPending,
    ackError,

    /**
     * Arm the modal from /auth/methods. The notice is shown on every visit: there is no
     * client-side "already acknowledged" shortcut, because the only acknowledgment that counts is
     * the one recorded server-side after sign-in.
     */
    applyMethods(methods: AuthMethods): void {
      if (!methods.login_banner_enabled) return;
      enabled.set(true);
      classification.set(
        (methods.login_banner_classification as BannerClassification) || 'UNCLASSIFIED'
      );
      consentPending.set(true);
    },

    /**
     * A hold can land after mount (the banner text changed under a live session). The hold
     * itself is proof the banner is enabled, better evidence than the /auth/methods probe whose
     * fail-closed default reports it OFF.
     */
    armForHold(): void {
      enabled.set(true);
      consentPending.set(true);
    },

    /**
     * Accept the banner. Two situations reach this, and only one can talk to the API:
     *
     * - **Signed in** (`hold`: a banner hold bounced us back here): the acknowledgment IS the
     *   remedy, so it must reach the API before any other route will answer. A failure is shown
     *   in place, because dismissing the modal would drop the user into an app where every
     *   request 403s.
     * - **Anonymous**: there is no session to record against yet. The modal is the AC-8 notice;
     *   the consent is POSTed by `recordConsent()` the moment sign-in produces a session.
     */
    async acknowledge(hold: boolean): Promise<AcknowledgeOutcome> {
      ackError.set('');

      if (hold) {
        ackPending.set(true);
        const result = await acknowledgeBanner();
        ackPending.set(false);

        if (!result.success) {
          ackError.set(result.message || get(t)('loginBanner.acknowledgeFailed'));
          return 'failed';
        }

        consentPending.set(false);
        return 'signed-in';
      }

      consentPending.set(false);
      return 'dismissed';
    },

    /**
     * Refuse the banner: end the session and return to a clean sign-in page (the old
     * `about:blank` navigation was commonly blocked, pinning the user with no feedback). The
     * notice reappears because consent is a precondition for access, not a dismissible dialog.
     * `blankFields` clears the page's own fields.
     */
    async decline(blankFields: () => void): Promise<void> {
      ackError.set('');
      ackPending.set(false);
      blankFields();

      // Clears the hold, the cookies and every user store; also empties the toast queue, which
      // is why the explanation below is raised afterwards.
      await logout();

      consentPending.set(get(enabled));
      toastStore.info(get(t)('loginBanner.mustAcknowledge'));
    },

    /**
     * Record banner consent now that a session exists. Called from every path that produces
     * one. Best-effort on purpose: if the write fails, the AC-8 gate refuses the next request,
     * the lifecycle interceptor publishes the hold, and the user lands back here with the banner
     * and a real error message, a strictly better outcome than blocking sign-in on a transient
     * failure.
     */
    async recordConsent(): Promise<void> {
      if (!get(enabled)) return;
      const result = await acknowledgeBanner();
      if (!result.success) {
        console.warn('Login.svelte: banner acknowledgment not recorded:', result.message);
      }
    },
  };
}
