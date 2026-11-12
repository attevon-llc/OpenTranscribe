<script lang="ts">
  import '$styles/auth.css';
  import { resolve } from '$app/paths';
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
  import { pageStore } from '$stores/pageStore';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import { verifyEmail, resendEmailVerification } from '$lib/api/invitations';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { onMount } from 'svelte';

  import logoBanner from '$assets/logo-banner.png';
  import FormField from '$components/ui/FormField.svelte';
  import Banner from '$components/ui/Banner.svelte';

  let phase: 'loading' | 'verified' | 'failed' = 'loading';
  /** Server prose for either outcome. Rendered as-is. */
  let outcomeMessage = '';

  let resendAddress = '';
  let resendPending = false;
  let resendNotice = '';

  $: tokenParam = $pageStore.url.searchParams.get('token') || '';

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
        <a href={resolve('/login')} class="auth-link">{$t('auth.backToLogin')}</a>
      </div>
    {:else}
      <Banner tone="error" size="base">{outcomeMessage}</Banner>

      <form on:submit|preventDefault={handleResend} class="auth-form">
        <p class="muted">{$t('auth.verifyEmail.resendDescription')}</p>

        <FormField id="resend-email" label={$t('auth.email')} size="lg">
          <input
            type="email"
            id="resend-email"
            bind:value={resendAddress}
            placeholder={$t('auth.emailPlaceholder')}
            autocomplete="email"
            disabled={resendPending}
            required
          />
        </FormField>

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
        <a href={resolve('/login')} class="auth-link">{$t('auth.backToLogin')}</a>
      </div>
    {/if}
  </div>
</div>

<style>
  .auth-card {
    max-width: 420px;
  }

  .auth-header {
    margin-bottom: var(--space-6);
  }

  .auth-form {
    gap: var(--space-5);
    margin-top: var(--space-6);
  }

  .auth-button {
    width: 100%;
  }

  .notice {
    margin-top: var(--space-4);
    padding: var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background-color: var(--background-color);
    color: var(--text-light);
    font-size: var(--font-size-md);
    text-align: center;
  }

  .centered {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: var(--space-3);
    padding: var(--space-6) 0;
  }

  .muted {
    color: var(--text-light);
    font-size: var(--font-size-body);
    margin: 0;
  }
</style>
