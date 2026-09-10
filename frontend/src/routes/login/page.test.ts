/**
 * `login/+page.svelte` is the community-edition sign-in route. Real behavior
 * found while reading the component (community build: `isCloudEdition` is
 * `false` from `import.meta.env.VITE_DEPLOYMENT_EDITION`, so the hosted-IdP
 * branch never runs here):
 *
 * - `onMount` awaits `getAuthMethods()` and stores the result. **Nothing that
 *   depends on the answer renders until it resolves** — the whole sign-in card
 *   is gated on an `authMethodsLoaded` flag and shows `.auth-methods-loading`
 *   until then. So a test may not query `#email` straight after `render()`;
 *   it has to await the gate opening (`renderLogin()` below does exactly
 *   that). The gate exists because painting against the local-only placeholder
 *   defaults and then inserting the SSO/PKI/register elements moved the submit
 *   button after paint — a real CLS defect, and the cause of 58
 *   non-deterministic auth E2E failures on 2026-09-06.
 * - The gate must open on FAILURE too. `authMethodsLoaded` is set in a
 *   `finally`, and the OIDC-callback preamble that runs ahead of the fetch is
 *   wrapped so an exception there cannot escape either. A login page pinned to
 *   a spinner by a transient backend blip is "nobody can sign in".
 * - Once the gate opens: the username/password form is gated on
 *   `authMethods.local_enabled || authMethods.ldap_enabled`
 *   (`credentialFormEnabled` — LDAP authenticates through the SAME form, not a
 *   separate one), and the SSO button row is gated on
 *   `authMethods.oidc_enabled || authMethods.pki_enabled`
 *   (`ssoButtonsEnabled`). The "forgot password" link is gated separately on
 *   `authMethods.local_enabled` alone (an LDAP-only deployment has no local
 *   passwords to reset).
 * - `handleSubmit` calls `login(email, password)` from `$stores/auth`.
 *   Invalid credentials do NOT render any inline error text in the DOM —
 *   there is no error-message element for this path. The component instead
 *   calls `toastStore.error(result.message)` and, only for `status === 401 ||
 *   403`, clears the password field and refocuses it (400/422 refocuses the
 *   email field instead — a malformed identifier, not a rejected one).
 * - On `result.success`, the page sets `loginSuccess = true` (which swaps the
 *   whole page for a "signing in..." transition — see the `{#if
 *   loginSuccess}` block) and, after a 600ms delay, calls
 *   `goto('/', { replaceState: true })`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';
import type { AuthMethods } from '$stores/auth';

const mockLogin = vi.hoisted(() => vi.fn());
const mockGetAuthMethods = vi.hoisted(() => vi.fn());
const mockLoginWithOIDC = vi.hoisted(() => vi.fn());
const mockLoginWithPKI = vi.hoisted(() => vi.fn());
const mockVerifyMFA = vi.hoisted(() => vi.fn());
const mockAcknowledgeBanner = vi.hoisted(() => vi.fn());
const mockLogout = vi.hoisted(() => vi.fn());
const mockChangeOwnPassword = vi.hoisted(() => vi.fn());
const mockLoginWithExternalAuth = vi.hoisted(() => vi.fn());
const mockHandleOIDCCallback = vi.hoisted(() => vi.fn());

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return {
    login: mockLogin,
    loginWithExternalAuth: mockLoginWithExternalAuth,
    authStore: writable({ user: null }),
    isAuthenticated: writable(false),
    getAuthMethods: mockGetAuthMethods,
    loginWithOIDC: mockLoginWithOIDC,
    handleOIDCCallback: mockHandleOIDCCallback,
    loginWithPKI: mockLoginWithPKI,
    verifyMFA: mockVerifyMFA,
    accountLifecycle: writable(null),
    clearAccountLifecycle: vi.fn(),
    changeOwnPassword: mockChangeOwnPassword,
    acknowledgeBanner: mockAcknowledgeBanner,
    logout: mockLogout,
  };
});

const mockToast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/api/invitations', () => ({ resendEmailVerification: vi.fn() }));

function noopComponent() {
  return () => {};
}
vi.mock('$lib/components/ClassificationBanner.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/LoginBanner.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/mfa/MfaEnrollment.svelte', () => ({ default: noopComponent() }));

import Page from './+page.svelte';
import { gotoCalls } from '../../test-mocks/app-navigation';

function authMethods(overrides: Partial<AuthMethods> = {}): AuthMethods {
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
    ...overrides,
  };
}

/** A promise whose settlement the test controls, for driving the gate by hand. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  gotoCalls.length = 0;
  mockGetAuthMethods.mockResolvedValue(authMethods());
  // The OIDC-callback preamble keys off `?code=&state=` and remembers handled
  // states in sessionStorage; a leftover URL or key would silently reroute an
  // unrelated test through that branch.
  window.history.replaceState({}, '', '/login');
  sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  window.history.replaceState({}, '', '/login');
  sessionStorage.clear();
});

/**
 * Render the page and wait for the auth-methods gate to open.
 *
 * `render()` alone yields the `.auth-methods-loading` placeholder, not the
 * form — querying `#email` at that point returns null and every subsequent
 * `fireEvent` dies with "Unable to fire a ... event - please provide a DOM
 * element", which reads as a broken test rather than as "the gate is shut".
 */
async function renderLogin() {
  const rendered = render(Page);
  await waitFor(() => {
    expect(rendered.container.querySelector('#email')).not.toBeNull();
  });
  return rendered;
}

async function fillAndSubmit(container: HTMLElement, email: string, password: string) {
  const emailInput = container.querySelector('#email') as HTMLInputElement | null;
  const passwordInput = container.querySelector('#password') as HTMLInputElement | null;
  const form = container.querySelector('.auth-form') as HTMLFormElement | null;
  // Named up front so a shut gate says so, instead of surfacing as a
  // Testing Library "provide a DOM element" stack trace inside fireEvent.
  expect(emailInput, 'credential form is not rendered: #email missing').not.toBeNull();
  expect(passwordInput, 'credential form is not rendered: #password missing').not.toBeNull();
  expect(form, 'credential form is not rendered: .auth-form missing').not.toBeNull();
  await fireEvent.input(emailInput!, { target: { value: email } });
  await fireEvent.input(passwordInput!, { target: { value: password } });
  await fireEvent.submit(form!);
}

describe('login/+page — credential submission', () => {
  it('invalid credentials: shows a toast (no inline error element for this path) and clears the password field', async () => {
    mockLogin.mockResolvedValue({
      success: false,
      status: 401,
      message: 'auth.error.invalidCredentials',
    });

    const { container } = await renderLogin();

    await fillAndSubmit(container, 'user@example.com', 'wrong-password');
    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith('user@example.com', 'wrong-password')
    );

    expect(mockToast.error).toHaveBeenCalledWith('auth.error.invalidCredentials');
    const passwordInput = container.querySelector('#password') as HTMLInputElement;
    expect(passwordInput.value).toBe('');
    // The credential form is still the thing on screen — no success transition,
    // no inline `.error-message`/`.field-error` element carries this failure.
    expect(container.querySelector('.login-success-fullpage')).toBeNull();
  });

  it('valid credentials: calls the auth store with trimmed input, then redirects home', async () => {
    mockLogin.mockResolvedValue({ success: true, must_change_password: false });

    const { container } = await renderLogin();

    await fillAndSubmit(container, '  user@example.com  ', 'correct-password');
    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith('user@example.com', 'correct-password')
    );

    // Success shows the full-page "signing in" transition immediately...
    await waitFor(() => expect(container.querySelector('.login-success-fullpage')).not.toBeNull());
    expect(gotoCalls).toEqual([]);

    // ...then redirects home after the documented 600ms delay.
    await vi.advanceTimersByTimeAsync(600);
    expect(gotoCalls).toEqual(['/']);
  });
});

/**
 * The gate itself. Everything here is about WHEN the card is painted, not what
 * it contains — the CLS/E2E-flake fix of 2026-09-06 and the fail-open contract
 * that has to come with it.
 */
describe('login/+page — the auth-methods gate', () => {
  it('paints a placeholder and NOTHING answer-dependent until /auth/methods resolves', async () => {
    const pending = deferred<AuthMethods>();
    mockGetAuthMethods.mockReturnValue(pending.promise);

    const { container } = render(Page);
    await waitFor(() => {
      expect(container.querySelector('.auth-methods-loading')).not.toBeNull();
    });

    // Deliberately an SSO + open-registration deployment: every one of these
    // elements DIFFERS from the local-only placeholder defaults, so if any of
    // them were on screen now it would have to move when the answer lands.
    // That movement is the defect.
    expect(container.querySelector('#email')).toBeNull();
    expect(container.querySelector('#password')).toBeNull();
    expect(container.querySelector('.auth-form')).toBeNull();
    expect(container.querySelector('.external-auth-buttons')).toBeNull();
    expect(container.querySelector('.forgot-password-link')).toBeNull();
    expect(container.querySelector('.auth-links')).toBeNull();
    expect(container.querySelector('.no-auth-methods')).toBeNull();

    // The placeholder is announced, not silent: a screen reader has to be told
    // the card is still resolving.
    const placeholder = container.querySelector('.auth-methods-loading');
    expect(placeholder?.getAttribute('aria-busy')).toBe('true');
    expect(placeholder?.getAttribute('aria-live')).toBe('polite');
    expect(placeholder?.textContent).toContain('auth.loadingSignInOptions');

    pending.resolve(
      authMethods({ oidc_enabled: true, pki_enabled: true, allow_registration: true })
    );

    // ...and when it opens, the card arrives complete and in its final shape.
    await waitFor(() => {
      expect(container.querySelector('#email')).not.toBeNull();
    });
    expect(container.querySelector('.auth-methods-loading')).toBeNull();
    expect(container.querySelector('.oidc-button')).not.toBeNull();
    expect(container.querySelector('.pki-button')).not.toBeNull();
    expect(container.querySelector('.forgot-password-link')).not.toBeNull();
    expect(container.querySelector('.auth-links')?.textContent).toContain('auth.register');
  });

  it('fails OPEN when getAuthMethods() rejects: a usable credential form, not a stuck spinner', async () => {
    mockGetAuthMethods.mockRejectedValue(new Error('backend unreachable'));
    mockLogin.mockResolvedValue({ success: true, must_change_password: false });

    const { container } = render(Page);
    await waitFor(() => {
      expect(container.querySelector('#email')).not.toBeNull();
    });
    expect(container.querySelector('.auth-methods-loading')).toBeNull();
    expect(container.querySelector('.no-auth-methods')).toBeNull();

    // "Rendered" is not "usable" — drive an actual sign-in through it. This is
    // the whole point of failing open: an operator whose /auth/methods probe
    // blipped must still be able to get into their own deployment.
    await fillAndSubmit(container, 'user@example.com', 'correct-password');
    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith('user@example.com', 'correct-password')
    );
    await waitFor(() => expect(container.querySelector('.login-success-fullpage')).not.toBeNull());
  });

  it('fails OPEN when the OIDC callback throws: the credential form, not a stuck spinner', async () => {
    // Returning from the IdP, and the callback exchange blows up rather than
    // answering `{ success: false }`. That preamble runs BEFORE the
    // getAuthMethods() fetch, so an escaping exception would skip the fetch —
    // and with it the `finally` that opens the gate — leaving the user pinned
    // to the spinner with no way to sign in at all.
    window.history.replaceState({}, '', '/login?code=abc123&state=st-oidc-throw');
    mockHandleOIDCCallback.mockRejectedValue(new Error('callback exchange failed'));
    mockLogin.mockResolvedValue({ success: true, must_change_password: false });

    const { container } = render(Page);
    await waitFor(() => {
      expect(container.querySelector('#email')).not.toBeNull();
    });
    expect(container.querySelector('.auth-methods-loading')).toBeNull();

    // The failure is reported rather than swallowed...
    expect(mockToast.error).toHaveBeenCalledWith('auth.loginFailed');
    // ...and the fallback path actually works.
    await fillAndSubmit(container, 'user@example.com', 'correct-password');
    await waitFor(() =>
      expect(mockLogin).toHaveBeenCalledWith('user@example.com', 'correct-password')
    );
  });

  it('fails OPEN when sessionStorage is blocked on the OIDC callback URL', async () => {
    // The realistic version of the case above, and not a mock of our own code:
    // a browser set to block site storage throws SecurityError on the mere
    // touch of `sessionStorage`. The OIDC preamble reads it unconditionally,
    // ahead of the fetch that opens the gate.
    window.history.replaceState({}, '', '/login?code=abc123&state=st-storage-blocked');
    const blocked = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('The operation is insecure.', 'SecurityError');
    });
    mockLogin.mockResolvedValue({ success: true, must_change_password: false });

    try {
      const { container } = render(Page);
      await waitFor(() => {
        expect(container.querySelector('#email')).not.toBeNull();
      });
      expect(container.querySelector('.auth-methods-loading')).toBeNull();

      await fillAndSubmit(container, 'user@example.com', 'correct-password');
      await waitFor(() =>
        expect(mockLogin).toHaveBeenCalledWith('user@example.com', 'correct-password')
      );
    } finally {
      blocked.mockRestore();
    }
  });
});

describe('login/+page — auth-method branches', () => {
  it('renders OIDC and PKI buttons alongside the credential form when both are enabled', async () => {
    mockGetAuthMethods.mockResolvedValue(
      authMethods({ oidc_enabled: true, pki_enabled: true, local_enabled: true })
    );

    const { container } = render(Page);
    await waitFor(() => {
      expect(container.querySelector('.oidc-button')).not.toBeNull();
    });

    // Real i18n text (from the mocked `$t`, which echoes the key), not just presence.
    expect(container.querySelector('.oidc-button')?.textContent).toContain('auth.loginWithOidc');
    expect(container.querySelector('.pki-button')?.textContent).toContain(
      'auth.loginWithCertificate'
    );
    expect(container.querySelectorAll('.external-auth-button')).toHaveLength(2);
    expect(container.querySelector('.auth-form')).not.toBeNull();
    expect(container.querySelector('.forgot-password-link')?.textContent).toContain(
      'auth.forgotPassword'
    );
  });

  it('an LDAP-only deployment shows the credential form but no SSO buttons and no forgot-password link', async () => {
    mockGetAuthMethods.mockResolvedValue(
      authMethods({
        local_enabled: false,
        ldap_enabled: true,
        oidc_enabled: false,
        pki_enabled: false,
      })
    );

    const { container } = render(Page);
    await waitFor(() => {
      // credentialFormEnabled = local_enabled || ldap_enabled -> true via ldap
      expect(container.querySelector('.auth-form')).not.toBeNull();
    });
    expect(container.querySelector('.oidc-button')).toBeNull();
    expect(container.querySelector('.pki-button')).toBeNull();
    expect(container.querySelector('.auth-divider')).toBeNull();
    // Self-service reset only exists for LOCAL passwords, not LDAP-owned ones.
    expect(container.querySelector('.forgot-password-link')).toBeNull();
  });

  it('no usable auth method at all: shows the "no auth methods" notice, not an empty form', async () => {
    mockGetAuthMethods.mockResolvedValue(
      authMethods({
        local_enabled: false,
        ldap_enabled: false,
        oidc_enabled: false,
        pki_enabled: false,
      })
    );

    const { container } = render(Page);
    await waitFor(() => {
      expect(container.querySelector('.no-auth-methods')).not.toBeNull();
    });
    expect(container.querySelector('.auth-form')).toBeNull();
    expect(container.querySelector('.external-auth-buttons')).toBeNull();
  });
});
