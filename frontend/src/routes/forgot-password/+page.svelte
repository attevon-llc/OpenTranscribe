<script lang="ts">
  import { goto } from '$app/navigation';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';

  import logoBanner from '../../assets/logo-banner.png';

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
    } catch (err: any) {
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
        <a href="/login" class="auth-link">{$t('auth.backToLogin')}</a>
      </div>
    {:else}
      <form on:submit|preventDefault={handleSubmit} class="auth-form">
        {#if errorMessage}
          <div class="error-message" role="alert">{errorMessage}</div>
        {/if}

        <div class="form-group">
          <label for="email">{$t('auth.email')}</label>
          <input
            type="email"
            id="email"
            bind:value={email}
            placeholder={$t('auth.emailPlaceholder')}
            autocomplete="email"
            required
          />
        </div>

        <button type="submit" class="auth-button" disabled={loading}>
          {#if loading}
            <span class="spinner"></span> {$t('auth.sending')}
          {:else}
            {$t('auth.sendResetLink')}
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

  .error-message {
    background-color: var(--error-color-light, #fef2f2);
    color: var(--error-color, #ef4444);
    padding: 0.75rem;
    border-radius: 4px;
    border: 1px solid rgba(239, 68, 68, 0.2);
    font-weight: 500;
  }

  .auth-logo {
    text-align: center;
    margin-bottom: 1.5rem;
  }

  .logo-image {
    max-width: 200px;
    height: auto;
  }

  .spinner {
    display: inline-block;
    width: 1rem;
    height: 1rem;
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-radius: 50%;
    border-top-color: #fff;
    animation: spin 1s ease-in-out infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
