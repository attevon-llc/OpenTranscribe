/**
 * `/auth/methods`: the public auth configuration, the fail-closed answer used when the
 * probe fails, and the short-lived shared request every caller on a page load joins.
 * Pure (no HTTP client): `$stores/auth` supplies the request.
 */

// Available authentication methods
export interface AuthMethods {
  methods: string[];
  oidc_enabled: boolean;
  /**
   * @deprecated Duplicate of `oidc_enabled`, emitted by the backend for one
   * minor release so a cached bundle of THIS file keeps working against an
   * upgraded backend. Never read it in new code.
   */
  keycloak_enabled?: boolean;
  pki_enabled: boolean;
  ldap_enabled: boolean;
  // Whether accounts holding a local password may sign in at all. Note this is
  // NOT the same as "show the username/password form" — LDAP authenticates
  // through that same form, so the form is gated on `local_enabled ||
  // ldap_enabled`.
  local_enabled: boolean;
  // Whether anyone may create their own account via POST /auth/register.
  allow_registration: boolean;
  mfa_enabled: boolean;
  mfa_required: boolean;
  login_banner_enabled: boolean;
  login_banner_text: string;
  login_banner_classification: string;
  /** Session limits in minutes (0 = off). Absent from an older backend. */
  session_idle_timeout_minutes?: number;
  session_absolute_timeout_minutes?: number;
}

/**
 * The configuration assumed when `/auth/methods` cannot be read.
 *
 * Fail-closed defaults when the probe fails. The two new flags are
 * deliberately ASYMMETRIC:
 *   local_enabled: true  — "closed" here means "still reachable". A
 *     self-hosted install whose /auth/methods probe fails (backend
 *     restarting, proxy hiccup) must still render the sign-in form, or a
 *     transient error locks every operator out of their own deployment.
 *     The backend is the authority and rejects the login anyway if local
 *     auth really is off.
 *   allow_registration: false — "closed" here means "offer nothing". A
 *     signup link we cannot confirm is enabled either 403s after the user
 *     fills the whole form, or advertises an open-registration surface the
 *     operator deliberately turned off. Both are worse than hiding it.
 */
export function failClosedAuthMethods(): AuthMethods {
  return {
    methods: ['local'],
    oidc_enabled: false,
    pki_enabled: false,
    ldap_enabled: false,
    local_enabled: true,
    allow_registration: false,
    mfa_enabled: false,
    mfa_required: false,
    login_banner_enabled: false,
    login_banner_text: '',
    login_banner_classification: 'UNCLASSIFIED',
  };
}

/**
 * One shared in-flight request whose successful answer stays fresh for `freshMs`.
 * Failures are never remembered: the next call asks again.
 */
export function sharedFreshRequest<T>(
  load: () => Promise<{ value: T; ok: boolean }>,
  freshMs: number
): { get: () => Promise<T>; invalidate: () => void } {
  let cache: { at: number; promise: Promise<T> } | null = null;

  return {
    get() {
      if (cache && Date.now() - cache.at < freshMs) {
        return cache.promise;
      }
      const request = load();
      const entry = { at: Date.now(), promise: request.then((r) => r.value) };
      cache = entry;
      void request.then((r) => {
        if (!r.ok && cache === entry) cache = null;
      });
      return entry.promise;
    },
    invalidate() {
      cache = null;
    },
  };
}
