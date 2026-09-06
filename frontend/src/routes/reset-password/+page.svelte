<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import axiosInstance from '$lib/axios';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { t } from '$stores/locale';

  import logoBanner from '../../assets/logo-banner.png';

  let newPassword = '';
  let confirmPassword = '';
  let loading = false;
  let errorMessage = '';
  let success = false;
  let showPassword = false;

  $: tokenParam = $page.url.searchParams.get('token') || '';

  function togglePasswordVisibility() {
    showPassword = !showPassword;
  }

  async function handleSubmit() {
    errorMessage = '';

    if (!tokenParam) {
      errorMessage = $t('auth.invalidResetLink');
      return;
    }

    if (!newPassword) {
      errorMessage = $t('auth.passwordRequired');
      return;
    }

    if (newPassword !== confirmPassword) {
      errorMessage = $t('auth.passwordsNoMatch');
      return;
    }

    loading = true;

    try {
      await axiosInstance.post('/auth/password-reset/confirm', {
        token: tokenParam,
        new_password: newPassword,
      });
      success = true;
    } catch (err) {
      errorMessage = getErrorMessage(err, $t('auth.resetPasswordFailed'));
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
      <h1>{$t('auth.resetPassword')}</h1>
      {#if !success}
        <p>{$t('auth.resetPasswordDescription')}</p>
      {/if}
    </div>

    {#if success}
      <div class="success-message" role="alert">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <polyline points="20,6 9,17 4,12"/>
        </svg>
        <span>{$t('auth.passwordResetSuccess')}</span>
      </div>

      <div class="auth-links">
        <a href="/login" class="auth-link">{$t('auth.backToLogin')}</a>
      </div>
    {:else if !tokenParam}
      <div class="error-message" role="alert">
        {$t('auth.invalidResetLink')}
      </div>
      <div class="auth-links">
        <a href="/forgot-password" class="auth-link">{$t('auth.requestNewLink')}</a>
      </div>
    {:else}
      <form on:submit|preventDefault={handleSubmit} class="auth-form">
        {#if errorMessage}
          <div class="error-message" role="alert">{errorMessage}</div>
        {/if}

        <div class="form-group">
          <div class="password-header">
            <label for="newPassword">{$t('auth.newPassword')}</label>
            <button
              type="button"
              class="toggle-password"
              on:click={togglePasswordVisibility}
              tabindex="-1"
              aria-label={showPassword ? 'Hide password' : 'Show password'}
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
          <input
            type={showPassword ? 'text' : 'password'}
            id="newPassword"
            bind:value={newPassword}
            placeholder={$t('auth.newPasswordPlaceholder')}
            autocomplete="new-password"
            required
          />
        </div>

        <div class="form-group">
          <label for="confirmPassword">{$t('auth.confirmPassword')}</label>
          <input
            type={showPassword ? 'text' : 'password'}
            id="confirmPassword"
            bind:value={confirmPassword}
            placeholder={$t('auth.confirmPasswordPlaceholder')}
            autocomplete="new-password"
            required
          />
        </div>

        <button type="submit" class="auth-button" disabled={loading}>
          {#if loading}
            <span class="spinner"></span> {$t('auth.resetting')}
          {:else}
            {$t('auth.resetPassword')}
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

  .auth-button {
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
