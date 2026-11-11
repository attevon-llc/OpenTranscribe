<script lang="ts">
  import '$styles/auth.css';
  import EyeIcon from '$components/ui/EyeIcon.svelte';
  import { resolve } from '$app/paths';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { register, login, getAuthMethods } from "$stores/auth";
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { loadPasswordPolicy, buildPasswordRequirements, FALLBACK_PASSWORD_POLICY, type PasswordPolicy } from '$lib/passwordPolicy';
  import Spinner from '$components/ui/Spinner.svelte';

  // Import logo asset for proper Vite processing
  import logoBanner from '$assets/logo-banner.png';
  import FormField from '$components/ui/FormField.svelte';

  // Self-registration is a deployment setting. Probe it before rendering the
  // form: without this the user fills every field and the backend answers 403.
  let registrationAllowed = false;
  let checkingRegistration = true;

  // Requirement bullets come from the server's active policy (nist / stig / custom), not a
  // hardcoded list: under nist there are no composition rules and the minimum is 15.
  let passwordPolicy: PasswordPolicy = FALLBACK_PASSWORD_POLICY;
  $: passwordRequirements = buildPasswordRequirements(passwordPolicy);
  onMount(() => {
    void loadPasswordPolicy().then((p) => (passwordPolicy = p));
  });

  onMount(() => {
    void (async () => {
      const methods = await getAuthMethods();
      registrationAllowed = methods.allow_registration;
      checkingRegistration = false;

      if (!registrationAllowed) {
        toastStore.error($t('auth.registrationDisabled'));
        void goto(resolve('/login'), { replace: true });
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
  // A rejection is shown only inline: the toast truncated to one line (hiding the policy reason),
  // and showing both put the same message on screen twice.
  let formError = '';

  async function handleSubmit() {
    loading = true;
    formError = '';

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
          void goto(resolve("/"));
        } else {
          toastStore.error($t('auth.registrationSuccessLoginFailed'));
        }
      } else {
        formError = (result.message || $t('auth.registrationFailed')).replace(/^Value error,\s*/, '');
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
        <Spinner label={$t('common.loading')} size="small" />
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
      <FormField id="username" label={$t('auth.username')} size="lg">
        <input
          id="username"
          type="text"
          bind:value={username}
          placeholder={$t('auth.usernamePlaceholder')}
          disabled={loading}
        />
      </FormField>

      <FormField id="email" label={$t('auth.email')} size="lg">
        <input
          id="email"
          type="email"
          bind:value={email}
          placeholder={$t('auth.emailPlaceholder')}
          disabled={loading}
        />
      </FormField>

      <FormField id="password" label={$t('auth.password')} size="lg">
        <svelte:fragment slot="label-after">
          <div class="info-tooltip">
            <svg class="info-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <path d="M12 16v-4"></path>
              <path d="M12 8h.01"></path>
            </svg>
            <div class="tooltip-content">
              <strong>{$t('auth.passwordRequirements')}</strong>
              <ul>
                {#each passwordRequirements as req (req.key)}
                  <li>{$t(req.key, req.params)}</li>
                {/each}
              </ul>
            </div>
          </div>
          <button
            type="button"
            class="toggle-password"
            on:click={togglePasswordVisibility}
            aria-label={showPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
            title={showPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
          >
            <EyeIcon visible={showPassword} />
          </button>
        </svelte:fragment>
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
      </FormField>

      <FormField id="confirmPassword" label={$t('auth.confirmPassword')} size="lg">
        <button
          slot="label-after"
          type="button"
          class="toggle-password"
          on:click={toggleConfirmPasswordVisibility}
          aria-label={showConfirmPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
          title={showConfirmPassword ? $t('auth.hidePassword') : $t('auth.showPassword')}
        >
          <EyeIcon visible={showConfirmPassword} />
        </button>
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
      </FormField>

      {#if formError}
        <p class="form-error" role="alert">{formError}</p>
      {/if}

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
            href={resolve('/login')}
            class="auth-link"
          >{$t('auth.login')}</a>
        </p>
      </div>
    </form>
    {/if}
  </div>
</div>

<style>
  .registration-check {
    display: flex;
    justify-content: center;
    padding: var(--space-8) 0;
  }

  .form-error {
    margin: 0 0 var(--space-3);
    font-size: var(--font-size-sm);
    line-height: 1.4;
    color: var(--error-text);
  }

  .auth-button:hover:not(:disabled) {
    box-shadow: var(--shadow-primary-hover);
  }

  .auth-button:active:not(:disabled) {
    transform: translateY(0);
  }

  .auth-footer {
    text-align: center;
  }

  .auth-link-text {
    color: var(--text-secondary);
    font-size: var(--font-size-body);
  }

  .auth-link {
    font-weight: var(--font-weight-medium);
  }

  .auth-logo .logo-banner {
    height: 60px;
    width: auto;
    object-fit: contain;
    border-radius: var(--radius-lg);
  }

  .toggle-password {
    margin-inline-start: auto;
    background: none;
    border: none;
    cursor: pointer;
    padding: var(--space-1);
    color: var(--text-light);
    display: flex;
    align-items: center;
    border-radius: var(--radius-sm);
    transition: background-color var(--duration-base);
  }

  .toggle-password:hover {
    background-color: var(--surface-hover);
  }

  .info-tooltip {
    position: relative;
    display: inline-flex;
    align-items: center;
  }

  .info-icon {
    color: var(--text-light);
    cursor: help;
    transition: color var(--duration-base);
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
    border-radius: var(--radius-lg);
    padding: var(--space-3) var(--space-4);
    box-shadow: var(--shadow-lg);
    width: 250px;
    z-index: var(--z-dropdown);
    opacity: 0;
    visibility: hidden;
    transition: opacity var(--duration-base), visibility var(--duration-base);
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
    margin-bottom: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .tooltip-content ul {
    margin: 0;
    padding-left: var(--space-5);
    font-size: var(--font-size-xs);
    color: var(--text-light);
  }

  .tooltip-content li {
    margin-bottom: var(--space-1);
  }

  .tooltip-content li:last-child {
    margin-bottom: 0;
  }
</style>
