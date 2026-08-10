<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { login, loginWithExternalAuth, authStore, isAuthenticated, getAuthMethods, loginWithOIDC, handleOIDCCallback, loginWithPKI, verifyMFA, accountLifecycle, clearAccountLifecycle, changeOwnPassword, acknowledgeBanner, logout, type AuthMethods } from "$stores/auth";
  import { resendEmailVerification } from '$lib/api/invitations';
  import { onMount, onDestroy } from 'svelte';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { browser } from '$app/environment';
  import { isCloudEdition } from '$lib/edition';
  import ClassificationBanner from '$lib/components/ClassificationBanner.svelte';
  import LoginBanner from '$components/LoginBanner.svelte';
  import MfaEnrollment from '$components/mfa/MfaEnrollment.svelte';
  import Spinner from '../../components/ui/Spinner.svelte';

  // Cloud edition: the hosted sign-in component mounts into this node; an
  // auth-state listener hydrates our local user store once a session exists.
  let externalSignInNode: HTMLElement | null = null;
  let externalUnmount: (() => void) | null = null;
  let externalUnlisten: (() => void) | null = null;
  let externalAuthLoading = isCloudEdition;

  // Import logo asset for proper Vite processing
  import logoBanner from '../../assets/logo-banner.png';

  // Form data
  let email = "";
  let password = "";
  let loading = false;
  let oidcLoading = false;
  let pkiLoading = false;
  let formSubmitted = false;
  let showPassword = false;
  let successMessage = "";
  let loginSuccess = false;

  // MFA state. `mfaToken` is a short-lived half-token: memory only, never
  // localStorage/sessionStorage, and cleared on every exit from the MFA steps.
  let mfaRequired = false;
  let mfaEnrollmentRequired = false;
  let mfaToken = "";
  let mfaCode = "";
  let mfaLoading = false;
  let useBackupCode = false;

  // Email verification (v375). `/auth/login` raises exactly ONE 403 — an
  // unverified address on a deployment that requires verification — so the HTTP
  // status identifies this state and no substring match on the localised
  // message is needed.
  let emailNotVerified = false;
  let resendPending = false;
  let resendNotice = "";

  // Forced password change (403 `detail.code === password_change_required`).
  // The session is alive; PUT /users/me is the only route that answers.
  let forcedCurrentPassword = "";
  let forcedNewPassword = "";
  let forcedConfirmPassword = "";
  let forcedChangeLoading = false;

  // Login banner (FedRAMP AC-8).
  //
  // Consent is a SERVER record — `POST /auth/banner/acknowledge` — because
  // `get_current_active_user` refuses every non-exempt route with
  // `detail.code === "banner_acknowledgment_required"` until that timestamp
  // exists and post-dates the current banner wording. The old
  // `sessionStorage['banner_acknowledged']` flag never reached the server, so a
  // deployment with the banner enabled was unusable after login.
  //
  // `bannerConsentPending` is in-memory ONLY, by design: it gates nothing but
  // this page's own modal, and persisting it is what made the fake look real.
  let bannerEnabled = false;
  let bannerText = "";
  let bannerClassification: 'UNCLASSIFIED' | 'CUI' | 'FOUO' | 'CONFIDENTIAL' | 'SECRET' | 'TOP SECRET' | 'TOP SECRET//SCI' = 'UNCLASSIFIED';
  let bannerConsentPending = false;
  let bannerAckPending = false;
  let bannerAckError = "";

  // A banner hold means we are ALREADY signed in and the gate bounced us here,
  // so acknowledging must hit the API rather than merely dismiss the modal.
  $: bannerHold = $accountLifecycle?.code === 'banner_acknowledgment_required';
  // `banner_text_changed`: the user did consent — to different wording. Saying so
  // is the difference between "the notice was updated" and "this is broken".
  $: bannerNoticeUpdated = $accountLifecycle?.reason === 'banner_text_changed';
  // The banner hold has its own UI (the consent modal), so it must not fall
  // through to the account-lifecycle panels below — which would show an expired
  // account screen for a user whose account is perfectly fine.
  $: lifecyclePanel = $accountLifecycle && !bannerHold ? $accountLifecycle : null;

  // A hold can land after mount (the banner text changed under a live session),
  // so re-arm the modal rather than assuming onMount saw it. The hold itself is
  // proof the banner is enabled — better evidence than the /auth/methods probe,
  // whose fail-closed default reports it OFF and would leave a held user staring
  // at a sign-in form that no longer works. LoginBanner fetches the current
  // wording from /auth/banner regardless of what we hold here.
  $: if (bannerHold && !bannerConsentPending) {
    bannerEnabled = true;
    bannerConsentPending = true;
  }

  // Authentication methods
  let authMethods: AuthMethods = {
    methods: ["local"],
    oidc_enabled: false,
    pki_enabled: false,
    ldap_enabled: false,
    local_enabled: true,
    allow_registration: false,
    mfa_enabled: false,
    mfa_required: false,
    login_banner_enabled: false,
    login_banner_text: "",
    login_banner_classification: "UNCLASSIFIED",
  };

  // The username/password form serves BOTH local accounts and LDAP — LDAP
  // credentials are posted to the same /auth/login endpoint — so it must not be
  // gated on `local_enabled` alone, or an LDAP-only deployment loses its only
  // way in.
  $: credentialFormEnabled = authMethods.local_enabled || authMethods.ldap_enabled;
  $: ssoButtonsEnabled = authMethods.oidc_enabled || authMethods.pki_enabled;

  // Validation
  let emailValid = true;
  let passwordValid = true;

  // Focus the email field on mount and fetch auth methods
  onMount(() => {
    let handleVisibilityChange: (() => void) | undefined;
    let handlePageShow: (() => void) | undefined;

    (async () => {
      // Reset loading states on mount (handles browser back button)
      oidcLoading = false;
      pkiLoading = false;
      loading = false;

      // Cloud edition: the hosted IdP owns login, registration, and MFA. Mount
      // its sign-in component and hydrate our store when it reports a session.
      // The community local-login / OIDC-callback flow below is skipped.
      if (isCloudEdition) {
        await setupExternalSignIn();
        return;
      }

      // Check for OIDC callback parameters
      const urlParams = new URLSearchParams(window.location.search);
      const code = urlParams.get('code');
      const state = urlParams.get('state');

      if (code && state) {
        // Clear URL parameters immediately to prevent double-processing on refresh
        window.history.replaceState({}, document.title, window.location.pathname);

        // Check if we already processed this callback (prevents double toast)
        const processedKey = `oidc_callback_${state}`;
        if (sessionStorage.getItem(processedKey)) {
          // Already processed this callback, skip
          window.location.href = "/";
          return;
        }
        sessionStorage.setItem(processedKey, 'true');

        // Handle the OIDC callback
        oidcLoading = true;
        const result = await handleOIDCCallback(code, state);
        oidcLoading = false;

        if (result.success) {
          loginSuccess = true;
          setTimeout(() => goto('/', { replaceState: true }), 600);
          return;
        } else {
          // Only show error if it's not a state-related issue (likely double-request)
          if (!result.message?.includes('state')) {
            toastStore.error(result.message || $t('auth.loginFailed'));
          } else {
            // State error but user might already be logged in, check and redirect
            if ($isAuthenticated) {
              window.location.href = "/";
              return;
            }
            toastStore.error(result.message || $t('auth.loginFailed'));
          }
        }
      }

      // Fetch available auth methods
      authMethods = await getAuthMethods();

      // Check for banner settings. The notice is shown on every visit: there is
      // no client-side "already acknowledged" shortcut any more, because the only
      // acknowledgment that counts is the one recorded server-side after sign-in.
      if (authMethods.login_banner_enabled) {
        bannerEnabled = true;
        bannerText = authMethods.login_banner_text || "";
        bannerClassification = (authMethods.login_banner_classification as typeof bannerClassification) || "UNCLASSIFIED";
        bannerConsentPending = true;
      }

      const emailInput = document.getElementById('email');
      if (emailInput && !bannerConsentPending) emailInput.focus();

      // Handle page visibility change (user returns via back button)
      handleVisibilityChange = () => {
        if (document.visibilityState === 'visible') {
          // Reset loading states when page becomes visible again
          oidcLoading = false;
          pkiLoading = false;
        }
      };

      handlePageShow = () => {
        oidcLoading = false;
        pkiLoading = false;
      };

      if (browser) {
        document.addEventListener('visibilitychange', handleVisibilityChange);
        // Also handle popstate (browser back/forward)
        window.addEventListener('pageshow', handlePageShow);
      }
    })();

    return () => {
      if (browser) {
        if (handleVisibilityChange) {
          document.removeEventListener('visibilitychange', handleVisibilityChange);
        }
        if (handlePageShow) {
          window.removeEventListener('pageshow', handlePageShow);
        }
      }
    };
  });

  // Cloud edition: load the hosted auth SDK, mount its sign-in component, and
  // hydrate the local store on session change. No-op in the community build
  // (isCloudEdition gate above; $lib/cloud is an inert stub there).
  async function setupExternalSignIn() {
    try {
      const { loadExternalAuth, mountSignIn, hasExternalSession, onAuthChange } =
        await import('$lib/cloud');
      const authHandle = await loadExternalAuth();
      if (!authHandle) {
        externalAuthLoading = false;
        toastStore.error($t('auth.loginFailed'));
        return;
      }

      // Already signed in (e.g. returning user) — hydrate and go.
      if (await hasExternalSession()) {
        await completeExternalLogin();
        return;
      }

      // React to sign-in completion (the IdP handles the credential + MFA flow).
      externalUnlisten = await onAuthChange(() => {
        void (async () => {
          if (!loginSuccess && (await hasExternalSession())) {
            await completeExternalLogin();
          }
        })();
      });

      if (externalSignInNode) {
        // afterSignInUrl/afterSignUpUrl keep the user in-app; the listener does
        // the store hydration + navigation.
        externalUnmount = await mountSignIn(externalSignInNode, {
          afterSignInUrl: '/',
          afterSignUpUrl: '/',
          signUpUrl: '/register',
        });
      }
      externalAuthLoading = false;
    } catch (err) {
      console.error('External sign-in setup failed:', err);
      externalAuthLoading = false;
      toastStore.error($t('auth.loginFailed'));
    }
  }

  // Hydrate local user store from /auth/me after the IdP reports a session.
  async function completeExternalLogin() {
    const result = await loginWithExternalAuth();
    if (result.success) {
      loginSuccess = true;
      import('$lib/prefetch').then(m => m.prefetchDashboardData()).catch(() => {});
      setTimeout(() => goto('/', { replaceState: true }), 600);
    } else {
      toastStore.error(result.message || $t('auth.loginFailed'));
    }
  }

  // Tear down the hosted component + listener on unmount (cloud only).
  onDestroy(() => {
    if (externalUnmount) externalUnmount();
    if (externalUnlisten) externalUnlisten();
  });

  // Validate login identifier (email or username for LDAP)
  /**
   * Validates a login identifier - can be email or username
   * @param {string} identifier - The email or username to validate
   * @returns {boolean} True if the identifier is valid, false otherwise
   */
  function validateLoginIdentifier(identifier: string) {
    // Accept either a valid email OR a username
    // Username regex is permissive to support various LDAP naming conventions:
    // - Alphanumeric, dots, underscores, hyphens
    // - Backslashes for DOMAIN\username format
    // - At signs for user@domain format (handled by email regex too)
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    const usernameRegex = /^[a-zA-Z0-9._\\@-]{2,}$/;
    const trimmed = String(identifier).trim();
    return emailRegex.test(trimmed) || usernameRegex.test(trimmed);
  }

  // Check form validity
  function validateForm() {
    formSubmitted = true;
    emailValid = email.trim() !== '' && validateLoginIdentifier(email);
    passwordValid = password.trim() !== '';

    return emailValid && passwordValid;
  }

  // Handle form submission
  async function handleSubmit() {
    successMessage = "";

    // Validate required fields first
    if (!email.trim()) {
      toastStore.error($t('auth.identifierRequired'));
      document.getElementById('email')?.focus();
      return;
    }

    if (!validateLoginIdentifier(email.trim())) {
      toastStore.error($t('auth.validIdentifierRequired'));
      document.getElementById('email')?.focus();
      return;
    }

    if (!password.trim()) {
      toastStore.error($t('auth.passwordRequired'));
      document.getElementById('password')?.focus();
      return;
    }

    if (!validateForm()) {
      return;
    }

    loading = true;

    try {
      // Call the login function from our auth store
      const result = await login(email.trim(), password);

      // Two different MFA challenges can come back here. Enrolment wins when
      // both flags are set: the account has no factor to verify yet, so the
      // TOTP prompt would have nothing to check against.
      if (result.mfa_required && result.mfa_token) {
        mfaToken = result.mfa_token;
        if (result.mfa_enrollment_required) {
          mfaEnrollmentRequired = true;
        } else {
          mfaRequired = true;
        }
        loading = false;
        return;
      }

      // The credentials were accepted but the address is unverified. Offer the
      // resend action instead of the generic "check your credentials" toast.
      if (!result.success && result.email_not_verified) {
        emailNotVerified = true;
        resendNotice = "";
        password = "";
        loading = false;
        return;
      }

      if (result.success) {
        // A session exists now, so the consent captured by the banner modal can
        // finally be recorded. Ahead of the forced-password-change branch below
        // because the server checks the banner FIRST — clearing the password
        // hold without this one would just produce a banner 403 next.
        await recordBannerConsent();

        // The account carries `must_change_password`: a session exists, but the
        // lifecycle gate refuses every route bar PUT /users/me until it clears.
        // Publishing the hold here shows the remedy immediately, instead of the
        // app shell failing its first request with a 403.
        if (result.must_change_password) {
          accountLifecycle.set({ code: 'password_change_required', message: '' });
          password = "";
          loading = false;
          return;
        }

        loginSuccess = true;
        loading = false;

        // Prefetch dashboard data while showing success state
        import('$lib/prefetch').then(m => m.prefetchDashboardData()).catch(() => {});

        // Brief delay so the success state is visible before navigation
        setTimeout(() => goto('/', { replaceState: true }), 600);
      } else {
        console.error('Login.svelte: Login failed:', result.message);
        toastStore.error(result.message || $t('auth.loginFailed'));

        // Steer focus from the HTTP status, never from the message text: the
        // message is localised, so matching English substrings ('email',
        // 'credentials', …) silently no-ops in the other seven locales.
        // 401/403 = the credentials were rejected; 400/422 = the identifier
        // itself was malformed.
        if (result.status === 401 || result.status === 403) {
          document.getElementById('password')?.focus();
          // Clear password on failed authentication for security
          password = "";
        } else if (result.status === 400 || result.status === 422) {
          document.getElementById('email')?.focus();
        }
      }
    } catch (err) {
      console.error("Login.svelte: Login error:", err);
      toastStore.error($t('auth.unexpectedError'));
    } finally {
      loading = false;
    }
  }

  // Toggle password visibility
  function togglePasswordVisibility() {
    showPassword = !showPassword;
  }

  /**
   * Ask for a fresh verification link.
   *
   * The endpoint always answers 200 with ONE constant message — for a
   * registered address, an unknown one, and an already-verified one alike — and
   * that message is rendered verbatim. Branching on the outcome (or reporting
   * "no such account") would rebuild the account-existence oracle the endpoint
   * exists to remove, and it needs no session to query.
   */
  async function handleResendVerification() {
    const address = email.trim();
    if (!address) {
      toastStore.error($t('auth.emailRequired'));
      document.getElementById('verify-email-address')?.focus();
      return;
    }

    resendPending = true;
    try {
      const { message } = await resendEmailVerification(address);
      resendNotice = message || $t('auth.verifyEmail.resendSent');
    } catch {
      // Transport/rate-limit failure — a condition of the request, not of the
      // address, so this text must not vary with who was typed in.
      resendNotice = $t('auth.verifyEmail.resendUnavailable');
    } finally {
      resendPending = false;
    }
  }

  /** Leave the unverified-address state and show the credential form again. */
  function dismissEmailNotVerified() {
    emailNotVerified = false;
    resendNotice = "";
    setTimeout(() => document.getElementById('password')?.focus(), 0);
  }

  /**
   * Clear `must_change_password` via the one route that still answers.
   *
   * On success the flag is cleared server-side, the lifecycle hold drops, and
   * the next request goes through — so we proceed exactly as after a login.
   */
  async function handleForcedPasswordChange() {
    if (!forcedCurrentPassword || !forcedNewPassword || !forcedConfirmPassword) {
      toastStore.error($t('auth.allFieldsRequired'));
      return;
    }
    if (forcedNewPassword !== forcedConfirmPassword) {
      toastStore.error($t('auth.passwordsNoMatch'));
      return;
    }

    forcedChangeLoading = true;
    const result = await changeOwnPassword(forcedCurrentPassword, forcedNewPassword);
    forcedChangeLoading = false;

    if (!result.success) {
      toastStore.error(result.message || $t('auth.forcedChange.failed'));
      return;
    }

    forcedCurrentPassword = "";
    forcedNewPassword = "";
    forcedConfirmPassword = "";
    toastStore.success($t('auth.forcedChange.success'));
    import('$lib/prefetch').then(m => m.prefetchDashboardData()).catch(() => {});
    // Navigate immediately rather than through the usual 600 ms "signing in"
    // flourish: the hold has already dropped, so the shell would render its
    // redirect placeholder over this page for the whole delay.
    goto('/', { replaceState: true });
  }

  /** Abandon the forced change: end the session and return to the sign-in form. */
  async function abandonForcedChange() {
    forcedCurrentPassword = "";
    forcedNewPassword = "";
    forcedConfirmPassword = "";
    password = "";
    await logout();
  }

  // Handle OIDC login with timeout
  async function handleOIDCLogin() {
    oidcLoading = true;

    try {
      // Add timeout to prevent infinite spinner if the provider is down
      const timeoutPromise = new Promise<{ success: false; message: string }>((_, reject) =>
        setTimeout(() => reject(new Error('Connection timeout')), 10000)
      );

      const result = await Promise.race([
        loginWithOIDC(),
        timeoutPromise
      ]);

      // If successful, the user is redirected to the identity provider
      // If failed, show error
      if (!result.success) {
        oidcLoading = false;
        toastStore.error(result.message || $t('auth.loginFailed'));
      }
      // Note: oidcLoading stays true during the redirect to the provider
    } catch (error) {
      oidcLoading = false;
      toastStore.error($t('auth.error.oidcUnreachable'));
    }
  }

  // Handle PKI login
  async function handlePKILogin() {
    pkiLoading = true;
    const result = await loginWithPKI();
    pkiLoading = false;

    if (result.success) {
      await recordBannerConsent();
      loginSuccess = true;
      setTimeout(() => goto('/', { replaceState: true }), 600);
    } else {
      toastStore.error(result.message || $t('auth.loginFailed'));
    }
  }

  // Handle MFA verification
  async function handleMFASubmit() {
    if (!mfaCode.trim()) {
      toastStore.error($t('auth.mfaCodeRequired'));
      return;
    }

    mfaLoading = true;

    try {
      const result = await verifyMFA(mfaToken, mfaCode.trim(), useBackupCode);

      if (result.success) {
        await recordBannerConsent();
        loginSuccess = true;
        setTimeout(() => goto('/', { replaceState: true }), 600);
      } else {
        toastStore.error(result.message || $t('auth.mfaVerificationFailed'));
        mfaCode = "";
      }
    } catch (err) {
      console.error("MFA verification error:", err);
      toastStore.error($t('auth.unexpectedError'));
      mfaCode = "";
    } finally {
      mfaLoading = false;
    }
  }

  // Cancel MFA and return to login
  function cancelMFA() {
    mfaRequired = false;
    mfaToken = "";
    mfaCode = "";
    useBackupCode = false;
    password = "";
  }

  // Enrolment finished: /mfa/verify-setup already set the session cookies and
  // the store is hydrated, so proceed exactly as after a normal login. Calling
  // /auth/login again here would start a second challenge.
  async function handleEnrollmentComplete() {
    mfaEnrollmentRequired = false;
    mfaToken = "";
    password = "";
    await recordBannerConsent();
    loginSuccess = true;
    import('$lib/prefetch').then(m => m.prefetchDashboardData()).catch(() => {});
    setTimeout(() => goto('/', { replaceState: true }), 600);
  }

  // Half-token spent/expired, or the user backed out — drop it and show the
  // credential form so they can mint a fresh one. The component owns the toast.
  function exitEnrollment() {
    mfaEnrollmentRequired = false;
    mfaToken = "";
    password = "";
    setTimeout(() => document.getElementById('password')?.focus(), 0);
  }

  /**
   * Accept the login banner.
   *
   * Two situations reach this, and only one of them can talk to the API:
   *
   * - **Signed in** (a banner hold bounced us back here): the acknowledgment IS
   *   the remedy, so it must reach `POST /auth/banner/acknowledge` before any
   *   other route will answer. A failure is shown in place — dismissing the modal
   *   would drop the user into an app where every request 403s.
   * - **Anonymous**: there is no session to record against yet. The modal is the
   *   AC-8 notice; the consent it captures is POSTed by `recordBannerConsent()`
   *   the moment sign-in produces a session.
   */
  async function handleBannerAcknowledge() {
    bannerAckError = "";

    if (bannerHold) {
      bannerAckPending = true;
      const result = await acknowledgeBanner();
      bannerAckPending = false;

      if (!result.success) {
        bannerAckError = result.message || $t('loginBanner.acknowledgeFailed');
        return;
      }

      bannerConsentPending = false;
      loginSuccess = true;
      import('$lib/prefetch').then(m => m.prefetchDashboardData()).catch(() => {});
      setTimeout(() => goto('/', { replaceState: true }), 600);
      return;
    }

    bannerConsentPending = false;
    setTimeout(() => document.getElementById('email')?.focus(), 100);
  }

  /**
   * Refuse the banner.
   *
   * This used to be `window.location.href = 'about:blank'`, which modern
   * browsers commonly block — leaving the user pinned to the banner with no
   * feedback whatsoever. End the session and return to a clean sign-in page
   * instead. The notice reappears because consent is a precondition for access,
   * not a dismissible dialog.
   */
  async function handleBannerDecline() {
    bannerAckError = "";
    bannerAckPending = false;
    email = "";
    password = "";
    emailNotVerified = false;
    mfaRequired = false;
    mfaEnrollmentRequired = false;
    mfaToken = "";
    mfaCode = "";

    // Clears the hold, the cookies and every user store; also empties the toast
    // queue, which is why the explanation below is raised afterwards.
    await logout();

    bannerConsentPending = bannerEnabled;
    toastStore.info($t('loginBanner.mustAcknowledge'));
  }

  /**
   * Record banner consent now that a session exists.
   *
   * Called from every path that produces one. Best-effort on purpose: if the
   * write fails, the AC-8 gate refuses the next request, the lifecycle
   * interceptor publishes the hold, and the user lands back here with the banner
   * and a real error message — a strictly better outcome than blocking the
   * sign-in on a transient failure.
   */
  async function recordBannerConsent() {
    if (!bannerEnabled) return;
    const result = await acknowledgeBanner();
    if (!result.success) {
      console.warn('Login.svelte: banner acknowledgment not recorded:', result.message);
    }
  }
</script>

<!-- Classification Banner -->
{#if bannerEnabled}
  <ClassificationBanner
    classification={bannerClassification}
    bannerText={bannerText}
    requireAcknowledgment={bannerConsentPending}
    position="top"
    pending={bannerAckPending}
    errorMessage={bannerAckError}
    noticeUpdated={bannerNoticeUpdated}
    on:acknowledge={handleBannerAcknowledge}
    on:decline={handleBannerDecline}
  />
{/if}

<!-- Login Banner Modal (shows consent dialog before login) -->
{#if loginSuccess}
  <!-- Full-page login success transition -->
  <div class="login-success-fullpage">
    <img src="/icons/icon-192x192.png" class="login-success-logo" alt="" />
    <Spinner size="small" />
    <p class="login-success-text">{$t('auth.signingIn')}</p>
  </div>
{:else}
{#if bannerConsentPending}
  <LoginBanner
    pending={bannerAckPending}
    errorMessage={bannerAckError}
    noticeUpdated={bannerNoticeUpdated}
    on:acknowledge={handleBannerAcknowledge}
    on:decline={handleBannerDecline}
  />
{/if}

<div class="auth-container" class:banner-offset={bannerEnabled}>
  <div class="auth-card">
    <div class="auth-header">
      <div class="auth-logo">
        <img src={logoBanner} alt="OpenTranscribe" class="logo-banner" />
      </div>
      <!-- The lifecycle and unverified-address panels carry their own heading;
           "Sign in to your account" would misdescribe them. -->
      {#if !lifecyclePanel && !emailNotVerified}
        <h1>{$t('auth.login')}</h1>
        <p>{$t('auth.signInToAccount')}</p>
      {/if}
    </div>
    {#if lifecyclePanel}
      <!-- Account-lifecycle hold. These states arrive as a 403 whose `detail` is
           an OBJECT carrying a machine-readable `code`; we branch on that code,
           never on the prose, which is server-owned and rendered as-is. The third
           code, `banner_acknowledgment_required`, is handled by the consent modal
           above and is excluded from `lifecyclePanel`. -->
      {#if lifecyclePanel.code === 'password_change_required'}
        <div class="lifecycle-panel">
          <h2>{$t('auth.forcedChange.title')}</h2>
          <p class="lifecycle-message">
            {lifecyclePanel.message || $t('auth.forcedChange.description')}
          </p>

          <form on:submit|preventDefault={handleForcedPasswordChange} class="auth-form">
            <div class="form-group">
              <label for="forced-current-password">{$t('auth.forcedChange.currentPassword')}</label>
              <input
                type="password"
                id="forced-current-password"
                bind:value={forcedCurrentPassword}
                autocomplete="current-password"
                disabled={forcedChangeLoading}
              />
            </div>

            <div class="form-group">
              <label for="forced-new-password">{$t('auth.newPassword')}</label>
              <input
                type="password"
                id="forced-new-password"
                bind:value={forcedNewPassword}
                placeholder={$t('auth.newPasswordPlaceholder')}
                autocomplete="new-password"
                disabled={forcedChangeLoading}
              />
            </div>

            <div class="form-group">
              <label for="forced-confirm-password">{$t('auth.confirmPassword')}</label>
              <input
                type="password"
                id="forced-confirm-password"
                bind:value={forcedConfirmPassword}
                placeholder={$t('auth.confirmPasswordPlaceholder')}
                autocomplete="new-password"
                disabled={forcedChangeLoading}
              />
            </div>

            <div class="password-policy">
              <strong>{$t('auth.passwordRequirements')}</strong>
              <ul>
                <li>{$t('auth.passwordReqLength')}</li>
                <li>{$t('auth.passwordReqUppercase')}</li>
                <li>{$t('auth.passwordReqLowercase')}</li>
                <li>{$t('auth.passwordReqNumber')}</li>
                <li>{$t('auth.passwordReqSpecial')}</li>
              </ul>
            </div>

            <button type="submit" class="auth-button" disabled={forcedChangeLoading}>
              {#if forcedChangeLoading}
                <Spinner size="small" color="white" /> {$t('auth.forcedChange.submitting')}
              {:else}
                {$t('auth.forcedChange.submit')}
              {/if}
            </button>
          </form>

          <div class="mfa-options">
            <button
              type="button"
              class="text-button cancel-button"
              on:click={abandonForcedChange}
              disabled={forcedChangeLoading}
            >
              {$t('nav.logout')}
            </button>
          </div>
        </div>
      {:else if lifecyclePanel.code === 'account_pending_approval'}
        <!-- Awaiting an administrator's decision (v379). The session is still
             live — the hold clears the instant the queue is worked — but no route
             is exempt from the gate, so this is a blocking screen rather than a
             toast that would otherwise fire once per refused request, forever.
             Logout must stay reachable or a pending user is stuck in the app with
             no way out. -->
        <div class="lifecycle-panel">
          <h2>{$t('auth.pendingApproval.title')}</h2>
          <p class="lifecycle-message">
            {lifecyclePanel.message || $t('auth.pendingApproval.description')}
          </p>
          <p class="lifecycle-hint">{$t('auth.pendingApproval.hint')}</p>
          <div class="mfa-options">
            <button type="button" class="text-button" on:click={() => window.location.reload()}>
              {$t('auth.pendingApproval.checkAgain')}
            </button>
            <button type="button" class="text-button cancel-button" on:click={logout}>
              {$t('nav.logout')}
            </button>
          </div>
        </div>
      {:else if lifecyclePanel.code === 'account_rejected'}
        <!-- Refused by an administrator. Unlike "pending" this bites whether or
             not approval is still required, the session has already been torn
             down, and there is nothing to wait for. Terminal by design. -->
        <div class="lifecycle-panel">
          <h2>{$t('auth.accountRejected.title')}</h2>
          <p class="lifecycle-message">
            {lifecyclePanel.message || $t('auth.accountRejected.description')}
          </p>
          <p class="lifecycle-hint">{$t('auth.accountRejected.contactAdmin')}</p>
          <div class="mfa-options">
            <button type="button" class="text-button" on:click={clearAccountLifecycle}>
              {$t('auth.backToLogin')}
            </button>
          </div>
        </div>
      {:else}
        <!-- Expired account: no self-service remedy exists, and the session has
             already been torn down. Terminal state by design. -->
        <div class="lifecycle-panel">
          <h2>{$t('auth.accountExpired.title')}</h2>
          <p class="lifecycle-message">
            {lifecyclePanel.message || $t('auth.accountExpired.description')}
          </p>
          <p class="lifecycle-hint">{$t('auth.accountExpired.contactAdmin')}</p>
          <div class="mfa-options">
            <button type="button" class="text-button" on:click={clearAccountLifecycle}>
              {$t('auth.backToLogin')}
            </button>
          </div>
        </div>
      {/if}
    {:else if isCloudEdition}
      <!-- Cloud edition: the hosted IdP owns login + registration + MFA. Its
           sign-in component mounts here; org context is resolved server-side
           from the IdP's org claim, and the IdP handles MFA factors itself. -->
      {#if externalAuthLoading}
        <div class="external-auth-loading">
          <Spinner size="small" />
          <p>{$t('auth.signingIn')}</p>
        </div>
      {/if}
      <div class="external-auth-mount" bind:this={externalSignInNode}></div>
    {:else}
    <!-- Forced MFA enrolment: the deployment requires a second factor and this
         account has none, so login returned an enrolment half-token instead of
         a session. There is no way past this step. -->
    {#if mfaEnrollmentRequired}
      <MfaEnrollment
        {mfaToken}
        on:complete={handleEnrollmentComplete}
        on:expired={exitEnrollment}
        on:cancel={exitEnrollment}
      />
    <!-- MFA Verification Form -->
    {:else if mfaRequired}
      <div class="mfa-form">
        <div class="mfa-icon">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
            <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
            <circle cx="12" cy="16" r="1"/>
          </svg>
        </div>
        <h2>{$t('auth.mfaRequired')}</h2>
        <p class="mfa-description">
          {#if useBackupCode}
            {$t('auth.mfaEnterBackupCode')}
          {:else}
            {$t('auth.mfaEnterCode')}
          {/if}
        </p>

        <form on:submit|preventDefault={handleMFASubmit} class="auth-form">
          <div class="form-group">
            <label for="mfaCode">
              {#if useBackupCode}
                {$t('auth.backupCode')}
              {:else}
                {$t('auth.mfaCode')}
              {/if}
            </label>
            <!-- svelte-ignore a11y_autofocus -->
            <input
              type="text"
              id="mfaCode"
              bind:value={mfaCode}
              placeholder={useBackupCode ? 'XXXX-XXXX' : '000000'}
              autocomplete="one-time-code"
              inputmode={useBackupCode ? 'text' : 'numeric'}
              pattern={useBackupCode ? '[A-Za-z0-9]{4}-[A-Za-z0-9]{4}' : '[0-9]{6}'}
              maxlength={useBackupCode ? 9 : 6}
              autofocus
            />
          </div>

          <button
            type="submit"
            class="auth-button"
            disabled={mfaLoading}
          >
            {#if mfaLoading}
              <Spinner size="small" color="white" /> {$t('auth.verifying')}
            {:else}
              {$t('auth.mfaVerify')}
            {/if}
          </button>
        </form>

        <div class="mfa-options">
          <button
            type="button"
            class="text-button"
            on:click={() => useBackupCode = !useBackupCode}
          >
            {#if useBackupCode}
              {$t('auth.useAuthenticatorApp')}
            {:else}
              {$t('auth.useBackupCode')}
            {/if}
          </button>
          <button
            type="button"
            class="text-button cancel-button"
            on:click={cancelMFA}
          >
            {$t('auth.cancel')}
          </button>
        </div>
      </div>
    {:else if emailNotVerified}
      <!-- Credentials accepted, address unverified. Distinct from a credential
           failure: there is nothing to retype, so offer the resend instead. -->
      <div class="lifecycle-panel">
        <h2>{$t('auth.verifyEmail.notVerifiedTitle')}</h2>
        <p class="lifecycle-message">{$t('auth.verifyEmail.notVerifiedDescription')}</p>

        <form on:submit|preventDefault={handleResendVerification} class="auth-form">
          <div class="form-group">
            <label for="verify-email-address">{$t('auth.email')}</label>
            <input
              type="email"
              id="verify-email-address"
              bind:value={email}
              placeholder={$t('auth.emailPlaceholder')}
              autocomplete="email"
              disabled={resendPending}
            />
          </div>

          <button type="submit" class="auth-button" disabled={resendPending}>
            {#if resendPending}
              <Spinner size="small" color="white" /> {$t('auth.verifyEmail.resending')}
            {:else}
              {$t('auth.verifyEmail.resend')}
            {/if}
          </button>
        </form>

        {#if resendNotice}
          <!-- Rendered verbatim and identically for every address: this notice
               must never imply whether the account exists. -->
          <p class="lifecycle-hint" role="status" aria-live="polite">{resendNotice}</p>
        {/if}

        <div class="mfa-options">
          <button type="button" class="text-button cancel-button" on:click={dismissEmailNotVerified}>
            {$t('auth.backToLogin')}
          </button>
        </div>
      </div>
    {:else}
      <!-- Normal Login Form. Hidden entirely when neither local nor LDAP
           credentials are accepted — otherwise the user fills it in and the
           backend rejects every submission. -->
      {#if credentialFormEnabled}
      <form on:submit|preventDefault={handleSubmit} class="auth-form">
      {#if successMessage}
        <div class="success-message" role="alert" aria-live="polite">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <polyline points="20,6 9,17 4,12"/>
          </svg>
          <span>{successMessage}</span>
        </div>
      {/if}

      <div class="form-group {!emailValid && formSubmitted ? 'has-error' : ''}">
        <label for="email">{$t('auth.emailOrUsername')}</label>
        <input
          type="text"
          id="email"
          bind:value={email}
          placeholder={$t('auth.emailOrUsernamePlaceholder')}
          aria-invalid={!emailValid && formSubmitted}
          autocomplete="username"
        />
        {#if !emailValid && formSubmitted}
          <div class="field-error">{$t('auth.validIdentifierRequired')}</div>
        {/if}
      </div>

      <div class="form-group {!passwordValid && formSubmitted ? 'has-error' : ''}">
        <div class="password-header">
          <label for="password">{$t('auth.password')}</label>
          <button
            type="button"
            class="toggle-password"
            on:click={togglePasswordVisibility}
            tabindex="-1"
            aria-label={showPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            title={showPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
          >
            {#if showPassword}
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
                <circle cx="12" cy="12" r="3"/>
              </svg>
            {:else}
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="m15 18-.722-3.25"/>
                <path d="m2 2 20 20"/>
                <path d="m9 9-.637 3.181"/>
                <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469"/>
                <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67"/>
                <path d="m18.147 8.476.853 3.524"/>
              </svg>
            {/if}
          </button>
        </div>
        {#if showPassword}
          <input
            type="text"
            id="password"
            bind:value={password}
            placeholder={$t('auth.passwordPlaceholder')}
            aria-invalid={!passwordValid && formSubmitted}
            autocomplete="current-password"
          />
        {:else}
          <input
            type="password"
            id="password"
            bind:value={password}
            placeholder={$t('auth.passwordPlaceholder')}
            aria-invalid={!passwordValid && formSubmitted}
            autocomplete="current-password"
          />
        {/if}
        {#if !passwordValid && formSubmitted}
          <div class="field-error">{$t('auth.passwordRequired')}</div>
        {/if}
      </div>

      <!-- Self-service reset only exists for passwords stored here. An
           LDAP-only deployment renders the form above but no reset link — the
           directory owns those credentials. -->
      {#if authMethods.local_enabled}
        <div class="forgot-password-row">
          <a href="/forgot-password" class="forgot-password-link">
            {$t('auth.forgotPassword')}
          </a>
        </div>
      {/if}

      <button
        type="submit"
        class="auth-button"
        disabled={loading}
      >
        {#if loading}
          <Spinner size="small" color="white" /> {$t('auth.signingIn')}
        {:else}
          {$t('auth.signIn')}
        {/if}
      </button>
    </form>
    {/if}

    {#if ssoButtonsEnabled}
      {#if credentialFormEnabled}
        <div class="auth-divider">
          <span>{$t('auth.orContinueWith')}</span>
        </div>
      {/if}

      <div class="external-auth-buttons">
        {#if authMethods.oidc_enabled}
          <button
            type="button"
            class="external-auth-button oidc-button"
            on:click={handleOIDCLogin}
            disabled={oidcLoading || loading}
          >
            {#if oidcLoading}
              <Spinner size="small" color="white" />
            {:else}
              <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
              </svg>
            {/if}
            <span>{$t('auth.loginWithOidc')}</span>
          </button>
        {/if}

        {#if authMethods.pki_enabled}
          <button
            type="button"
            class="external-auth-button pki-button"
            on:click={handlePKILogin}
            disabled={pkiLoading || loading}
          >
            {#if pkiLoading}
              <Spinner size="small" color="white" />
            {:else}
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
                <circle cx="12" cy="16" r="1"/>
                <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
              </svg>
            {/if}
            <span>{$t('auth.loginWithCertificate')}</span>
          </button>
        {/if}
      </div>
    {/if}

    {#if !credentialFormEnabled && !ssoButtonsEnabled}
      <p class="no-auth-methods" role="alert">{$t('auth.noAuthMethodsAvailable')}</p>
    {/if}

    <!-- Only advertise signup when the backend actually accepts it; otherwise
         the user fills the whole registration form and gets a 403. -->
    {#if authMethods.allow_registration}
      <div class="auth-links">
        <span class="auth-link-text">{$t('auth.needAccountPrefix')} <a
          href="/register"
          class="auth-link"
        >{$t('auth.register')}</a></span>
      </div>
    {/if}
    {/if}
    {/if}
  </div>
</div>
{/if}

<style>
  /* Cloud edition: hosted sign-in component mount target. */
  .external-auth-mount {
    display: flex;
    justify-content: center;
  }

  .external-auth-loading {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    padding: 1.5rem 0;
    color: var(--text-light);
    font-size: 0.9rem;
  }

  .auth-container {
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    min-height: 100dvh;
    padding: 1rem;
    background-color: var(--background-color);
  }

  .auth-card {
    background-color: var(--surface-color);
    border-radius: 8px;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    width: 100%;
    max-width: 400px;
    padding: 2rem;
  }

  .auth-header {
    text-align: center;
    margin-bottom: 2rem;
  }

  .auth-header h1 {
    font-size: 1.5rem;
    color: var(--text-color);
    margin-bottom: 0.5rem;
  }

  .auth-header p {
    color: var(--text-light);
    font-size: 0.9rem;
  }

  .auth-form {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .form-group label {
    font-size: 0.9rem;
    font-weight: 500;
  }

  .form-group input {
    padding: 0.75rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 1rem;
    transition: border-color 0.2s;
  }

  .form-group input:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .auth-button {
    background-color: #3b82f6;
    color: white;
    border: none;
    border-radius: 10px;
    padding: 0.6rem 1.2rem;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
  }

  .auth-button:hover:not(:disabled) {
    background-color: #2563eb;
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .auth-button:active:not(:disabled) {
    transform: translateY(0);
  }

  .auth-button:disabled {
    background-color: var(--border-color);
    cursor: not-allowed;
  }


  /* Shown when the deployment advertises no usable sign-in method at all. */
  .no-auth-methods {
    margin: 0;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--background-color);
    color: var(--text-light);
    font-size: 0.9rem;
    text-align: center;
  }

  .auth-links {
    margin-top: 1.5rem;
    text-align: center;
  }

  .auth-link-text {
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .auth-link {
    color: var(--primary-color);
    text-decoration: none;
    font-weight: 500;
  }

  .auth-link:hover {
    text-decoration: underline;
  }

  .forgot-password-row {
    text-align: right;
    margin-top: -0.5rem;
  }

  .forgot-password-link {
    color: var(--text-light);
    font-size: 0.85rem;
    text-decoration: none;
  }

  .forgot-password-link:hover {
    color: var(--primary-color, #3b82f6);
    text-decoration: underline;
  }

  .password-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .toggle-password {
    background: none;
    border: none;
    cursor: pointer;
    padding: 4px;
    color: var(--text-light);
    display: flex;
    align-items: center;
    border-radius: 4px;
    transition: background-color 0.2s;
  }

  .toggle-password:hover {
    background-color: var(--surface-hover, rgba(0, 0, 0, 0.05));
  }

  .field-error {
    color: var(--error-color);
    font-size: 0.85rem;
    margin-top: 0.25rem;
  }

  .has-error input {
    border-color: var(--error-color);
  }

  .success-message {
    background-color: var(--success-color-light);
    color: var(--success-color);
    padding: 0.75rem;
    border-radius: 4px;
    border: 1px solid rgba(34, 197, 94, 0.2);
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 500;
  }

  .success-message svg {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .auth-logo {
    text-align: center;
    margin-bottom: 1.5rem;
  }

  .auth-logo .logo-banner {
    height: 60px;
    width: auto;
    object-fit: contain;
    border-radius: 8px;
  }

  /* Divider for external auth */
  .auth-divider {
    display: flex;
    align-items: center;
    margin: 1.5rem 0;
  }

  .auth-divider::before,
  .auth-divider::after {
    content: "";
    flex: 1;
    height: 1px;
    background-color: var(--border-color);
  }

  .auth-divider span {
    padding: 0 1rem;
    color: var(--text-light);
    font-size: 0.85rem;
    white-space: nowrap;
  }

  /* External auth buttons */
  .external-auth-buttons {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .external-auth-button {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
    width: 100%;
    padding: 0.75rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .external-auth-button:hover:not(:disabled) {
    background-color: var(--surface-hover, rgba(0, 0, 0, 0.03));
    border-color: var(--primary-color);
  }

  .external-auth-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .external-auth-button svg {
    flex-shrink: 0;
  }

  .oidc-button {
    border-color: #4d4d4d;
  }

  .oidc-button:hover:not(:disabled) {
    border-color: #666;
    background-color: rgba(77, 77, 77, 0.05);
  }

  .pki-button {
    border-color: #059669;
  }

  .pki-button:hover:not(:disabled) {
    border-color: #10b981;
    background-color: rgba(5, 150, 105, 0.05);
  }

  .pki-button svg {
    color: #059669;
  }

  /* Banner offset for classification banner */
  .banner-offset {
    padding-top: 30px;
  }

  /* Account-lifecycle / email-verification panels. Colours come from theme vars
     so light and dark stay in parity. */
  .lifecycle-panel h2 {
    font-size: 1.25rem;
    color: var(--text-color);
    margin-bottom: 0.5rem;
    text-align: center;
  }

  .lifecycle-message {
    color: var(--text-light);
    font-size: 0.9rem;
    margin-bottom: 1.5rem;
    text-align: center;
  }

  .lifecycle-hint {
    margin-top: 1rem;
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--background-color);
    color: var(--text-light);
    font-size: 0.85rem;
    text-align: center;
  }

  .password-policy {
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--background-color);
    color: var(--text-light);
    font-size: 0.8rem;
  }

  .password-policy ul {
    margin: 0.5rem 0 0;
    padding-left: 1.1rem;
  }

  .password-policy li {
    margin-bottom: 0.2rem;
  }

  /* MFA Form Styles */
  .mfa-form {
    text-align: center;
  }

  .mfa-icon {
    margin-bottom: 1rem;
    color: var(--primary-color);
  }

  .mfa-form h2 {
    font-size: 1.25rem;
    color: var(--text-color);
    margin-bottom: 0.5rem;
  }

  .mfa-description {
    color: var(--text-light);
    font-size: 0.9rem;
    margin-bottom: 1.5rem;
  }

  .mfa-form .form-group {
    text-align: left;
  }

  .mfa-form input {
    text-align: center;
    font-size: 1.25rem;
    letter-spacing: 0.25em;
    font-family: monospace;
  }

  .mfa-options {
    margin-top: 1.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .text-button {
    background: none;
    border: none;
    color: var(--primary-color);
    font-size: 0.9rem;
    cursor: pointer;
    padding: 0.5rem;
    transition: color 0.2s;
  }

  .text-button:hover {
    color: var(--primary-color-dark, #2563eb);
    text-decoration: underline;
  }

  .text-button.cancel-button {
    color: var(--text-light);
  }

  .text-button.cancel-button:hover {
    color: var(--text-color);
  }

  /* Login success transition state */
  .login-success-fullpage {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 1.25rem;
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--bg-primary, #f8fafc);
  }

  .login-success-logo {
    width: 64px;
    height: 64px;
    border-radius: 12px;
    animation: login-success-pulse 1.5s ease-in-out infinite;
  }

  .login-success-text {
    color: var(--text-light);
    font-size: 0.95rem;
    font-weight: 500;
    margin: 0;
  }

  @keyframes login-success-pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.7; transform: scale(0.97); }
  }
</style>
