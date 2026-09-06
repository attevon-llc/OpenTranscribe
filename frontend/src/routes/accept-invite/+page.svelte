<script lang="ts">
  /**
   * /accept-invite?token=… — redeem an admin invitation.
   *
   * Unauthenticated by definition: the account does not exist until this page
   * succeeds. Two rules the backend depends on:
   *
   * 1. The token goes in the request BODY, never a header or a query string on
   *    the API call — it is a bearer credential for a not-yet-existing account.
   * 2. Unknown, expired, revoked and already-used tokens all answer with ONE
   *    identical message. We render it as-is and never branch on it; a missing
   *    token is posted too, so even that state is indistinguishable from the
   *    outside. Anything else turns this page into a token oracle.
   */
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { t } from '$stores/locale';
  import { loginWithOIDC, loginWithPKI } from '$stores/auth';
  import { toastStore } from '$stores/toast';
  import Spinner from '$components/ui/Spinner.svelte';
  import {
    lookupInvitation,
    acceptInvitation,
    type InvitationLookup,
    type InvitationAcceptResult,
  } from '$lib/api/invitations';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { onMount } from 'svelte';

  import logoBanner from '../../assets/logo-banner.png';

  let phase: 'loading' | 'form' | 'error' | 'done' = 'loading';
  /** Server-supplied prose, rendered verbatim. Never inspected. */
  let errorMessage = '';
  let invitation: InvitationLookup | null = null;
  let accepted: InvitationAcceptResult | null = null;

  let fullName = '';
  let password = '';
  let confirmPassword = '';
  let showPassword = false;
  let submitting = false;
  let ssoPending = false;

  $: tokenParam = $page.url.searchParams.get('token') || '';

  onMount(() => {
    void loadInvitation();
  });

  async function loadInvitation() {
    phase = 'loading';
    try {
      // Posted even when the query string carried no token: letting the server
      // answer keeps "no token" indistinguishable from "bad token".
      invitation = await lookupInvitation(tokenParam);
      fullName = invitation.full_name || '';
      phase = 'form';
    } catch (err) {
      errorMessage = getErrorMessage(err, $t('auth.acceptInvite.invalidLink'));
      phase = 'error';
    }
  }

  async function handleSubmit() {
    if (!invitation) return;

    if (invitation.requires_password) {
      if (!password || !confirmPassword) {
        toastStore.error($t('auth.allFieldsRequired'));
        return;
      }
      if (password !== confirmPassword) {
        toastStore.error($t('auth.passwordsNoMatch'));
        return;
      }
    }

    submitting = true;
    try {
      // An external account holds no local password, so the field is OMITTED
      // rather than sent empty — the backend refuses a password on those types.
      accepted = await acceptInvitation({
        token: tokenParam,
        full_name: fullName.trim() || undefined,
        ...(invitation.requires_password ? { password } : {}),
      });
      password = '';
      confirmPassword = '';
      phase = 'done';
    } catch (err) {
      // Covers a weak password and a token that expired mid-form alike; both
      // arrive as a 400 and both are shown exactly as the server worded them.
      errorMessage = getErrorMessage(err, $t('auth.acceptInvite.acceptFailed'));
      toastStore.error(errorMessage);
    } finally {
      submitting = false;
    }
  }

  async function handleSso() {
    if (!accepted) return;
    ssoPending = true;
    const result =
      accepted.auth_type === 'oidc' ? await loginWithOIDC() : await loginWithPKI();
    ssoPending = false;
    if (!result.success) {
      toastStore.error(result.message || $t('auth.loginFailed'));
    }
  }
</script>

<div class="auth-container">
  <div class="auth-card">
    <div class="auth-logo">
      <img src={logoBanner} alt="OpenTranscribe" class="logo-image" />
    </div>

    {#if phase === 'loading'}
      <div class="centered">
        <Spinner size="small" />
        <p class="muted">{$t('auth.acceptInvite.checking')}</p>
      </div>
    {:else if phase === 'error'}
      <div class="auth-header">
        <h1>{$t('auth.acceptInvite.title')}</h1>
      </div>
      <div class="error-message" role="alert">{errorMessage}</div>
      <div class="auth-links">
        <a href="/login" class="auth-link">{$t('auth.backToLogin')}</a>
      </div>
    {:else if phase === 'done' && accepted}
      <div class="auth-header">
        <h1>{$t('auth.acceptInvite.readyTitle')}</h1>
      </div>
      <div class="success-message" role="alert">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="20,6 9,17 4,12" />
        </svg>
        <span>{accepted.message}</span>
      </div>

      {#if accepted.can_login_with_password}
        <button type="button" class="auth-button" on:click={() => goto('/login')}>
          {$t('auth.signIn')}
        </button>
      {:else if accepted.auth_type === 'oidc' || accepted.auth_type === 'pki'}
        <!-- Externally-owned identity: the credential lives with the provider,
             so hand the user straight to it instead of to a password form. -->
        <button type="button" class="auth-button" on:click={handleSso} disabled={ssoPending}>
          {#if ssoPending}
            <Spinner size="small" color="white" />
          {/if}
          {accepted.auth_type === 'oidc'
            ? $t('auth.loginWithOidc')
            : $t('auth.loginWithCertificate')}
        </button>
      {:else}
        <!-- LDAP authenticates through the ordinary sign-in form. -->
        <button type="button" class="auth-button" on:click={() => goto('/login')}>
          {$t('auth.signIn')}
        </button>
      {/if}
    {:else if invitation}
      <div class="auth-header">
        <h1>{$t('auth.acceptInvite.title')}</h1>
        <p>{$t('auth.acceptInvite.description', { email: invitation.email })}</p>
      </div>

      <form on:submit|preventDefault={handleSubmit} class="auth-form">
        <div class="form-group">
          <label for="invite-full-name">{$t('auth.acceptInvite.fullName')}</label>
          <input
            type="text"
            id="invite-full-name"
            bind:value={fullName}
            placeholder={$t('auth.usernamePlaceholder')}
            autocomplete="name"
            disabled={submitting}
          />
        </div>

        {#if invitation.requires_password}
          <div class="form-group">
            <div class="password-header">
              <label for="invite-password">{$t('auth.acceptInvite.createPassword')}</label>
              <button
                type="button"
                class="toggle-password"
                on:click={() => (showPassword = !showPassword)}
                tabindex="-1"
                aria-label={showPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
                title={showPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
              >
                {#if showPassword}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                {:else}
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="m15 18-.722-3.25" />
                    <path d="m2 2 20 20" />
                    <path d="m9 9-.637 3.181" />
                    <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469" />
                    <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67" />
                    <path d="m18.147 8.476.853 3.524" />
                  </svg>
                {/if}
              </button>
            </div>
            <input
              type={showPassword ? 'text' : 'password'}
              id="invite-password"
              bind:value={password}
              placeholder={$t('auth.choosePassword')}
              autocomplete="new-password"
              disabled={submitting}
            />
          </div>

          <div class="form-group">
            <label for="invite-confirm-password">{$t('auth.confirmPassword')}</label>
            <input
              type={showPassword ? 'text' : 'password'}
              id="invite-confirm-password"
              bind:value={confirmPassword}
              placeholder={$t('auth.confirmPasswordPlaceholder')}
              autocomplete="new-password"
              disabled={submitting}
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
        {:else}
          <p class="sso-note">
            {$t('auth.acceptInvite.externalNote', { provider: invitation.auth_type })}
          </p>
        {/if}

        <button type="submit" class="auth-button" disabled={submitting}>
          {#if submitting}
            <Spinner size="small" color="white" /> {$t('auth.acceptInvite.accepting')}
          {:else}
            {$t('auth.acceptInvite.accept')}
          {/if}
        </button>
      </form>

      <div class="auth-links">
        <a href="/login" class="auth-link">{$t('auth.backToLogin')}</a>
      </div>
    {/if}
  </div>
</div>

<style>
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
    max-width: 420px;
    padding: 2rem;
  }

  .auth-logo {
    text-align: center;
    margin-bottom: 1.5rem;
  }

  .logo-image {
    max-width: 200px;
    height: auto;
  }

  .auth-header {
    text-align: center;
    margin-bottom: 1.5rem;
  }

  .auth-header h1 {
    font-size: 1.5rem;
    color: var(--text-color);
    margin-bottom: 0.5rem;
  }

  .auth-header p {
    color: var(--text-light);
    font-size: 0.9rem;
    word-break: break-word;
  }

  .auth-form {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .form-group label {
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .form-group input {
    padding: 0.75rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 1rem;
    transition: border-color 0.2s;
  }

  .form-group input:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .password-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
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

  .password-policy,
  .sso-note {
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--background-color);
    color: var(--text-light);
    font-size: 0.8rem;
    margin: 0;
  }

  .password-policy ul {
    margin: 0.5rem 0 0;
    padding-left: 1.1rem;
  }

  .password-policy li {
    margin-bottom: 0.2rem;
  }

  .auth-button {
    width: 100%;
    background-color: var(--primary-color);
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
  }

  .auth-button:disabled {
    background-color: var(--border-color);
    cursor: not-allowed;
  }

  .auth-links {
    margin-top: 1.5rem;
    text-align: center;
    color: var(--text-light);
  }

  .auth-link {
    color: var(--primary-color, #3b82f6);
    text-decoration: none;
  }

  .auth-link:hover {
    text-decoration: underline;
  }

  .error-message {
    background-color: var(--error-color-light, #fef2f2);
    color: var(--error-color, #ef4444);
    padding: 0.75rem;
    border-radius: 4px;
    border: 1px solid rgba(239, 68, 68, 0.2);
    font-weight: 500;
  }

  .success-message {
    background-color: var(--success-color-light, #f0fdf4);
    color: var(--success-color, #22c55e);
    padding: 0.75rem;
    border-radius: 4px;
    border: 1px solid rgba(34, 197, 94, 0.2);
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-weight: 500;
    margin-bottom: 1.5rem;
  }

  .centered {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    padding: 1.5rem 0;
  }

  .muted {
    color: var(--text-light);
    font-size: 0.9rem;
    margin: 0;
  }
</style>
