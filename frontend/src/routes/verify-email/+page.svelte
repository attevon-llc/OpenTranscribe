<script lang="ts">
  /**
   * /verify-email?token=… — redeem an email-verification token.
   *
   * Unauthenticated by design: a user who cannot log in until their address is
   * verified obviously cannot authenticate to verify it.
   *
   * Two server invariants this page must not undo:
   *
   * 1. Unknown, used and expired tokens answer with ONE identical message. It is
   *    rendered verbatim; nothing here inspects it.
   * 2. The resend route ALWAYS answers 200 with one constant message, for a
   *    registered address, an unknown one and an already-verified one alike. The
   *    UI must never imply which — that would be an account-existence oracle
   *    reachable with no session.
   */
  import { page } from '$app/stores';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import { verifyEmail, resendEmailVerification } from '$lib/api/invitations';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { onMount } from 'svelte';

  import logoBanner from '../../assets/logo-banner.png';

  let phase: 'loading' | 'verified' | 'failed' = 'loading';
  /** Server prose for either outcome. Rendered as-is. */
  let outcomeMessage = '';

  let resendAddress = '';
  let resendPending = false;
  let resendNotice = '';

  $: tokenParam = $page.url.searchParams.get('token') || '';

  onMount(() => {
    void runVerification();
  });

  async function runVerification() {
    phase = 'loading';
    try {
      // Posted even with an empty token so "no token" is indistinguishable from
      // "bad token" — the server owns that judgement, not this page.
      const { message } = await verifyEmail(tokenParam);
      outcomeMessage = message || $t('auth.verifyEmail.verified');
      phase = 'verified';
    } catch (err) {
      outcomeMessage = getErrorMessage(err, $t('auth.verifyEmail.failed'));
      phase = 'failed';
    }
  }

  async function handleResend() {
    const address = resendAddress.trim();
    if (!address) return;

    resendPending = true;
    try {
      const { message } = await resendEmailVerification(address);
      resendNotice = message || $t('auth.verifyEmail.resendSent');
    } catch {
      // A transport or rate-limit failure is a property of the REQUEST, not of
      // the address, so this text must not vary with who was typed in.
      resendNotice = $t('auth.verifyEmail.resendUnavailable');
    } finally {
      resendPending = false;
    }
  }
</script>

<div class="auth-container">
  <div class="auth-card">
    <div class="auth-logo">
      <img src={logoBanner} alt="OpenTranscribe" class="logo-image" />
    </div>

    <div class="auth-header">
      <h1>{$t('auth.verifyEmail.title')}</h1>
    </div>

    {#if phase === 'loading'}
      <div class="centered">
        <Spinner size="small" />
        <p class="muted">{$t('auth.verifyEmail.verifying')}</p>
      </div>
    {:else if phase === 'verified'}
      <div class="success-message" role="alert">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="20,6 9,17 4,12" />
        </svg>
        <span>{outcomeMessage}</span>
      </div>
      <div class="auth-links">
        <a href="/login" class="auth-link">{$t('auth.backToLogin')}</a>
      </div>
    {:else}
      <div class="error-message" role="alert">{outcomeMessage}</div>

      <form on:submit|preventDefault={handleResend} class="auth-form">
        <p class="muted">{$t('auth.verifyEmail.resendDescription')}</p>

        <div class="form-group">
          <label for="resend-email">{$t('auth.email')}</label>
          <input
            type="email"
            id="resend-email"
            bind:value={resendAddress}
            placeholder={$t('auth.emailPlaceholder')}
            autocomplete="email"
            disabled={resendPending}
            required
          />
        </div>

        <button type="submit" class="auth-button" disabled={resendPending || !resendAddress.trim()}>
          {#if resendPending}
            <Spinner size="small" color="white" /> {$t('auth.verifyEmail.resending')}
          {:else}
            {$t('auth.verifyEmail.resend')}
          {/if}
        </button>
      </form>

      {#if resendNotice}
        <!-- One notice for every address. Do not add a "no such account" branch. -->
        <p class="notice" role="status" aria-live="polite">{resendNotice}</p>
      {/if}

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

  .auth-form {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
    margin-top: 1.5rem;
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
  }

  .notice {
    margin-top: 1rem;
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--background-color);
    color: var(--text-light);
    font-size: 0.85rem;
    text-align: center;
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
