/**
 * `login/+page.svelte` is the community-edition sign-in route. Real behavior
 * found while reading the component (community build: `isCloudEdition` is
 * `false` from `import.meta.env.VITE_DEPLOYMENT_EDITION`, so the hosted-IdP
 * branch never runs here):
 *
 * - `onMount` awaits `getAuthMethods()` and stores the result; the
 *   username/password form is gated on `authMethods.local_enabled ||
 *   authMethods.ldap_enabled` (`credentialFormEnabled` — LDAP authenticates
 *   through the SAME form, not a separate one), and the SSO button row is
 *   gated on `authMethods.oidc_enabled || authMethods.pki_enabled`
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

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  gotoCalls.length = 0;
  mockGetAuthMethods.mockResolvedValue(authMethods());
});

afterEach(() => {
  vi.useRealTimers();
});

async function fillAndSubmit(container: HTMLElement, email: string, password: string) {
  const emailInput = container.querySelector('#email') as HTMLInputElement;
  const passwordInput = container.querySelector('#password') as HTMLInputElement;
  await fireEvent.input(emailInput, { target: { value: email } });
  await fireEvent.input(passwordInput, { target: { value: password } });
  const form = container.querySelector('.auth-form') as HTMLFormElement;
  await fireEvent.submit(form);
}

describe('login/+page — credential submission', () => {
  it('invalid credentials: shows a toast (no inline error element for this path) and clears the password field', async () => {
    mockLogin.mockResolvedValue({
      success: false,
      status: 401,
      message: 'auth.error.invalidCredentials',
    });

    const { container } = render(Page);
    await vi.waitFor(() => expect(mockGetAuthMethods).toHaveBeenCalled());

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

    const { container } = render(Page);
    await vi.waitFor(() => expect(mockGetAuthMethods).toHaveBeenCalled());

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
