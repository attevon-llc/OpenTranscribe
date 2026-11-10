<script lang="ts">
  import { resolve } from '$app/paths';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';

  import logoBanner from '$assets/logo-banner.png';
  import FormField from '$components/ui/FormField.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import Banner from '$components/ui/Banner.svelte';

  let email = '';
  let loading = false;
  let submitted = false;
  let errorMessage = '';

  async function handleSubmit() {
    if (!email.trim()) {
      errorMessage = 'Please enter your email address.';
      return;
    }

    loading = true;
    errorMessage = '';

    try {
      await axiosInstance.post('/auth/password-reset/request', { email: email.trim() });
      submitted = true;
    } catch {
      // Always show success to prevent email enumeration
      submitted = true;
    } finally {
      loading = false;
    }
  }
</script>

<div class="auth-container">
  <div class="auth-card">
    <div class="auth-logo">
      <img src={logoBanner} alt="OpenTranscribe" class="logo-image" />
    </div>

    <div class="auth-header">
      <h1>{$t('auth.forgotPassword')}</h1>
      <p>{$t('auth.forgotPasswordDescription')}</p>
    </div>

    {#if submitted}
      <div class="success-message" role="alert">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="20,6 9,17 4,12"/>
        </svg>
        <span>{$t('auth.resetLinkSent')}</span>
      </div>

      <div class="auth-links">
        <a href={resolve('/login')} class="auth-link">{$t('auth.backToLogin')}</a>
      </div>
    {:else}
      <form on:submit|preventDefault={handleSubmit} class="auth-form">
        {#if errorMessage}
          <Banner tone="error">{errorMessage}</Banner>
        {/if}

        <FormField id="email" label={$t('auth.email')} size="lg">
          <input
            type="email"
            id="email"
            bind:value={email}
            placeholder={$t('auth.emailPlaceholder')}
            autocomplete="email"
            required
          />
        </FormField>

        <button type="submit" class="auth-button" disabled={loading}>
          {#if loading}
            <Spinner size="inline" color="white" /> {$t('auth.sending')}
          {:else}
            {$t('auth.sendResetLink')}
          {/if}
        </button>
      </form>

      <div class="auth-links">
        <a href={resolve('/login')} class="auth-link">{$t('auth.backToLogin')}</a>
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
    padding: var(--space-4);
    background-color: var(--background-color);
  }

  .auth-card {
    background-color: var(--surface-color);
    border-radius: var(--radius-lg);
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    width: 100%;
    max-width: 400px;
    padding: var(--space-8);
  }

  .auth-header {
    text-align: center;
    margin-bottom: var(--space-8);
  }

  .auth-header h1 {
    font-size: var(--font-size-2xl);
    color: var(--text-color);
    margin-bottom: var(--space-2);
  }

  .auth-header p {
    color: var(--text-light);
    font-size: 0.9rem;
  }

  .auth-form {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }

  .auth-form input {
    font-size: var(--font-size-base);
  }

  .auth-button {
    background-color: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    border-radius: var(--radius-xl);
    padding: 0.6rem 1.2rem;
    font-size: 0.95rem;
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    transition: all var(--duration-base) ease;
    box-shadow: var(--shadow-primary);
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
  }

  .auth-button:hover:not(:disabled) {
    background-color: var(--primary-color);
    transform: translateY(-1px);
  }

  .auth-button:disabled {
    background-color: var(--border-color);
    cursor: not-allowed;
  }

  .auth-links {
    margin-top: var(--space-6);
    text-align: center;
    color: var(--text-light);
  }

  .auth-link {
    color: var(--primary-on-surface);
    text-decoration: none;
  }

  .auth-link:hover {
    text-decoration: underline;
  }

  .success-message {
    background-color: var(--success-color-light);
    color: var(--success-color);
    padding: var(--space-3);
    border-radius: var(--radius-sm);
    border: 1px solid rgba(34, 197, 94, 0.2);
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-weight: var(--font-weight-medium);
  }

  .auth-logo {
    text-align: center;
    margin-bottom: var(--space-6);
  }

  .logo-image {
    max-width: 200px;
    height: auto;
  }
</style>
