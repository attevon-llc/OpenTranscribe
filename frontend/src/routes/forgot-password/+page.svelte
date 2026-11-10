<script lang="ts">
  import '$styles/auth.css';
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
          <Banner tone="error" size="base">{errorMessage}</Banner>
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
    min-height: 100vh;
  }

</style>
