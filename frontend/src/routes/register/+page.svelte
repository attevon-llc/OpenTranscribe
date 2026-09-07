<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { register, login, getAuthMethods } from "$stores/auth";
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';

  // Import logo asset for proper Vite processing
  import logoBanner from '../../assets/logo-banner.png';

  // Self-registration is a deployment setting. Probe it before rendering the
  // form: without this the user fills every field and the backend answers 403.
  let registrationAllowed = false;
  let checkingRegistration = true;

  onMount(() => {
    (async () => {
      const methods = await getAuthMethods();
      registrationAllowed = methods.allow_registration;
      checkingRegistration = false;

      if (!registrationAllowed) {
        toastStore.error($t('auth.registrationDisabled'));
        goto('/login', { replaceState: true });
      }
    })();
  });

  // Form data
  let username = "";
  let email = "";
  let password = "";
  let confirmPassword = "";
  let loading = false;
  let showPassword = false;
  let showConfirmPassword = false;

  // Validate form
  function validateForm() {
    if (!username || !email || !password || !confirmPassword) {
      toastStore.error($t('auth.allFieldsRequired'));
      return false;
    }

    if (password !== confirmPassword) {
      toastStore.error($t('auth.passwordsNoMatch'));
      return false;
    }

    if (password.length < 8) {
      toastStore.error($t('auth.passwordMinLength'));
      return false;
    }

    // Simple email validation
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(email)) {
      toastStore.error($t('auth.validEmailRequired'));
      return false;
    }

    return true;
  }

  // Handle form submission
  async function handleSubmit() {
    loading = true;

    if (!validateForm()) {
      loading = false;
      return;
    }

    try {
      // Make sure we're using the right parameters in the right order:
      // register(email, fullName, password) is expected by our auth store
      const result = await register(email, username, password);

      if (result.success) {
        // Log the user in automatically after registration
        const loginResult = await login(email, password);
        if (loginResult.success) {
          toastStore.success($t('auth.registrationSuccess'));
          goto("/");
        } else {
          toastStore.error($t('auth.registrationSuccessLoginFailed'));
        }
      } else {
        toastStore.error(result.message || $t('auth.registrationFailed'));
      }
    } catch (err) {
      console.error("Registration error:", err);
      toastStore.error($t('auth.unexpectedError'));
    } finally {
      loading = false;
    }
  }

  // Toggle password visibility
  function togglePasswordVisibility() {
    showPassword = !showPassword;
  }

  function toggleConfirmPasswordVisibility() {
    showConfirmPassword = !showConfirmPassword;
  }
</script>

<div class="auth-container">
  <div class="auth-card">
    {#if checkingRegistration}
      <div class="registration-check">
        <Spinner size="small" />
      </div>
    {:else if registrationAllowed}
    <div class="auth-header">
      <div class="auth-logo">
        <img src={logoBanner} alt="OpenTranscribe" class="logo-banner" />
      </div>
      <h1>{$t('auth.register')}</h1>
      <p>{$t('auth.createNewAccount')}</p>
    </div>

    <form on:submit|preventDefault={handleSubmit} class="auth-form">
      <div class="form-group">
        <label for="username">{$t('auth.username')}</label>
        <input
          id="username"
          type="text"
          bind:value={username}
          placeholder={$t('auth.usernamePlaceholder')}
          disabled={loading}
        />
      </div>

      <div class="form-group">
        <label for="email">{$t('auth.email')}</label>
        <input
          id="email"
          type="email"
          bind:value={email}
          placeholder={$t('auth.emailPlaceholder')}
          disabled={loading}
        />
      </div>

      <div class="form-group">
        <div class="password-header">
          <div class="label-with-info">
            <label for="password">{$t('auth.password')}</label>
            <div class="info-tooltip">
              <svg class="info-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <path d="M12 16v-4"></path>
                <path d="M12 8h.01"></path>
              </svg>
              <div class="tooltip-content">
                <strong>{$t('auth.passwordRequirements')}</strong>
                <ul>
                  <li>{$t('auth.passwordReqLength')}</li>
                  <li>{$t('auth.passwordReqUppercase')}</li>
                  <li>{$t('auth.passwordReqLowercase')}</li>
                  <li>{$t('auth.passwordReqNumber')}</li>
                  <li>{$t('auth.passwordReqSpecial')}</li>
                </ul>
              </div>
            </div>
          </div>
          <button
            type="button"
            class="toggle-password"
            on:click={togglePasswordVisibility}
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
            id="password"
            type="text"
            bind:value={password}
            placeholder={$t('auth.choosePassword')}
            disabled={loading}
          />
        {:else}
          <input
            id="password"
            type="password"
            bind:value={password}
            placeholder={$t('auth.choosePassword')}
            disabled={loading}
          />
        {/if}
      </div>

      <div class="form-group">
        <div class="password-header">
          <label for="confirmPassword">{$t('auth.confirmPassword')}</label>
          <button
            type="button"
            class="toggle-password"
            on:click={toggleConfirmPasswordVisibility}
            aria-label={showConfirmPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            title={showConfirmPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
          >
            {#if showConfirmPassword}
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
        {#if showConfirmPassword}
          <input
            id="confirmPassword"
            type="text"
            bind:value={confirmPassword}
            placeholder={$t('auth.confirmPasswordPlaceholder')}
            disabled={loading}
          />
        {:else}
          <input
            id="confirmPassword"
            type="password"
            bind:value={confirmPassword}
            placeholder={$t('auth.confirmPasswordPlaceholder')}
            disabled={loading}
          />
        {/if}
      </div>

      <button
        type="submit"
        class="auth-button"
        disabled={loading}
      >
        {loading ? $t('auth.creatingAccount') : $t('auth.createAccount')}
      </button>

      <div class="auth-footer">
        <p class="auth-link-text">
          {$t('auth.haveAccountPrefix')} <a
            href="/login"
            class="auth-link"
          >{$t('auth.login')}</a>
        </p>
      </div>
    </form>
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
    max-width: 400px;
    padding: 2rem;
  }

  .registration-check {
    display: flex;
    justify-content: center;
    padding: 2rem 0;
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
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .auth-button:active:not(:disabled) {
    transform: translateY(0);
  }

  .auth-button:disabled {
    background-color: var(--border-color);
    cursor: not-allowed;
  }

  .auth-footer {
    text-align: center;
  }

  .auth-link-text {
    color: var(--text-secondary);
    font-size: 0.9rem;
  }

  .auth-link {
    color: var(--primary-on-surface);
    text-decoration: none;
    font-weight: 500;
  }

  .auth-link:hover {
    text-decoration: underline;
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

  .label-with-info {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .info-tooltip {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  .info-icon {
    color: var(--text-light);
    cursor: help;
    transition: color 0.2s;
  }

  .info-icon:hover {
    color: var(--primary-on-surface);
  }

  .tooltip-content {
    position: absolute;
    bottom: calc(100% + 8px);
    left: 50%;
    transform: translateX(-50%);
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.75rem 1rem;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    width: 250px;
    z-index: 100;
    opacity: 0;
    visibility: hidden;
    transition: opacity 0.2s, visibility 0.2s;
  }

  .info-tooltip:hover .tooltip-content {
    opacity: 1;
    visibility: visible;
  }

  .tooltip-content::after {
    content: '';
    position: absolute;
    top: 100%;
    left: 50%;
    transform: translateX(-50%);
    border: 6px solid transparent;
    border-top-color: var(--border-color);
  }

  .tooltip-content strong {
    display: block;
    margin-bottom: 0.5rem;
    font-size: 0.8rem;
    color: var(--text-color);
  }

  .tooltip-content ul {
    margin: 0;
    padding-left: 1.25rem;
    font-size: 0.75rem;
    color: var(--text-light);
  }

  .tooltip-content li {
    margin-bottom: 0.25rem;
  }

  .tooltip-content li:last-child {
    margin-bottom: 0;
  }
</style>
