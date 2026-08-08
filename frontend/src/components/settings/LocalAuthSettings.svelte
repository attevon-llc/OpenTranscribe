<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  // All fields come from a single config object (saved under "local" category)
  export let config: Record<string, any> = {};

  const dispatch = createEventDispatcher();

  // Helper to get value with default - only use default if value is truly undefined
  function getVal<T>(key: string, defaultVal: T): T {
    const val = config[key];
    return val !== undefined ? val : defaultVal;
  }

  // Every key below must be the name the BACKEND reads (app/core/auth_settings.py).
  // Four fields used to write frontend-only aliases — password_require_numbers,
  // mfa_issuer, max_login_attempts, lockout_duration_minutes — which were stored
  // faithfully and then never consulted by anything, so the panel silently did
  // nothing. Defaults below mirror the backend's coded defaults so a fresh
  // install shows the truth rather than an invented policy.
  function defaults() {
    return {
      local_enabled: getVal('local_enabled', true),
      allow_registration: getVal('allow_registration', true),
      // Both default false, matching the backend: turning either on is an opt-in
      // policy change, not something an upgrade should apply to a running
      // deployment and strand everybody behind.
      require_email_verification: getVal('require_email_verification', false),
      require_account_approval: getVal('require_account_approval', false),
      // Password policy
      password_min_length: getVal('password_min_length', 12),
      password_require_uppercase: getVal('password_require_uppercase', true),
      password_require_lowercase: getVal('password_require_lowercase', true),
      password_require_digit: getVal('password_require_digit', true),
      password_require_special: getVal('password_require_special', true),
      password_max_age_days: getVal('password_max_age_days', 60),
      // FedRAMP IA-5 requires 24 remembered passwords, not 5.
      password_history_count: getVal('password_history_count', 24),
      // MFA
      mfa_enabled: getVal('mfa_enabled', false),
      mfa_required: getVal('mfa_required', false),
      mfa_issuer_name: getVal('mfa_issuer_name', 'OpenTranscribe'),
      mfa_backup_code_count: getVal('mfa_backup_code_count', 10),
      mfa_token_expire_minutes: getVal('mfa_token_expire_minutes', 5),
      // Account lockout
      account_lockout_enabled: getVal('account_lockout_enabled', true),
      account_lockout_threshold: getVal('account_lockout_threshold', 5),
      account_lockout_duration_minutes: getVal('account_lockout_duration_minutes', 15),
      account_lockout_progressive: getVal('account_lockout_progressive', true),
      account_lockout_max_duration_minutes: getVal('account_lockout_max_duration_minutes', 1440),
      // Login banner (FedRAMP AC-8) — enforced at login, not merely displayed.
      login_banner_enabled: getVal('login_banner_enabled', false),
      login_banner_text: getVal('login_banner_text', ''),
      login_banner_classification: getVal('login_banner_classification', 'UNCLASSIFIED')
    };
  }

  let formData = defaults();

  let saving = false;

  // Update formData when config changes
  $: if (config) {
    formData = defaults();
  }

  // Server-side refusal: self-registration creates a LOCAL password account, so
  // it cannot be enabled while local password login is off. Warn before saving
  // rather than letting the save bounce.
  $: registrationConflict = formData.allow_registration && !formData.local_enabled;

  function handleChange() {
    dispatch('change');
  }

  function handleSave() {
    saving = true;
    dispatch('save', formData);
    setTimeout(() => saving = false, 500);
  }

  function getPasswordStrengthPreview(): string {
    const requirements: string[] = [];
    if (formData.password_min_length > 0) {
      requirements.push(`${formData.password_min_length}+ ${$t('settings.localAuth.characters')}`);
    }
    if (formData.password_require_uppercase) requirements.push($t('settings.localAuth.uppercase'));
    if (formData.password_require_lowercase) requirements.push($t('settings.localAuth.lowercase'));
    if (formData.password_require_digit) requirements.push($t('settings.localAuth.numbers'));
    if (formData.password_require_special) requirements.push($t('settings.localAuth.specialChars'));
    return requirements.join(', ');
  }
</script>

<div class="settings-panel">
  <div class="enable-toggle">
    <label class="toggle-label">
      <input
        type="checkbox"
        bind:checked={formData.local_enabled}
        on:change={handleChange}
      />
      <span class="toggle-text">{$t('settings.localAuth.enableLocal')}</span>
    </label>
    <span class="help-text">{$t('settings.localAuth.enableLocalHelp')}</span>
  </div>

  {#if registrationConflict}
    <!-- Lives outside the greyed-out registration section on purpose: that
         section is dimmed and pointer-events:none the moment local login is
         switched off, which is exactly when this warning matters. -->
    <p class="warning-banner" role="alert">{$t('settings.localAuth.registrationRequiresLocal')}</p>
  {/if}

  <div class="section" class:disabled={!formData.local_enabled}>
    <h3>{$t('settings.localAuth.registrationSettings')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.allow_registration}
        on:change={handleChange}
        disabled={!formData.local_enabled}
      />
      <span>{$t('settings.localAuth.allowSelfRegistration')}</span>
    </label>
    <span class="help-text indented">{$t('settings.localAuth.selfRegistrationHelp')}</span>

    <!-- Local-login specific: the gate lives in the password login path
         (`assert_email_verified_for_local_login`), which is why it is dimmed with
         the rest of this section when local passwords are off. -->
    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.require_email_verification}
        on:change={handleChange}
        disabled={!formData.local_enabled}
      />
      <span>{$t('settings.localAuth.requireEmailVerification')}</span>
    </label>
    <span class="help-text indented">{$t('settings.localAuth.requireEmailVerificationHelp')}</span>
  </div>

  <!--
    Deliberately NOT inside the registration section above: that block is dimmed
    and `pointer-events: none` when local passwords are off, but approval applies
    to EVERY newly provisioned account — self-registration and every external-IdP
    JIT path alike. A deployment that authenticates only through OIDC still needs
    this switch.
  -->
  <div class="section">
    <h3>{$t('settings.localAuth.accountAdmission')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.require_account_approval}
        on:change={handleChange}
      />
      <span>{$t('settings.localAuth.requireAccountApproval')}</span>
    </label>
    <span class="help-text indented">{$t('settings.localAuth.requireAccountApprovalHelp')}</span>
    {#if formData.require_account_approval}
      <p class="info-note">{$t('settings.localAuth.requireAccountApprovalQueueHint')}</p>
    {/if}
  </div>

  <div class="section" class:disabled={!formData.local_enabled}>
    <h3>{$t('settings.localAuth.passwordPolicy')}</h3>

    <div class="policy-preview">
      <strong>{$t('settings.localAuth.currentRequirements')}</strong> {getPasswordStrengthPreview()}
    </div>

    <div class="form-group">
      <label for="password_min_length">{$t('settings.localAuth.minPasswordLength')}</label>
      <input
        id="password_min_length"
        type="number"
        bind:value={formData.password_min_length}
        on:input={handleChange}
        min="8"
        max="128"
        disabled={!formData.local_enabled}
      />
    </div>

    <div class="checkbox-grid">
      <label class="checkbox-label">
        <input
          type="checkbox"
          bind:checked={formData.password_require_uppercase}
          on:change={handleChange}
          disabled={!formData.local_enabled}
        />
        <span>{$t('settings.localAuth.requireUppercase')}</span>
      </label>

      <label class="checkbox-label">
        <input
          type="checkbox"
          bind:checked={formData.password_require_lowercase}
          on:change={handleChange}
          disabled={!formData.local_enabled}
        />
        <span>{$t('settings.localAuth.requireLowercase')}</span>
      </label>

      <label class="checkbox-label">
        <input
          type="checkbox"
          bind:checked={formData.password_require_digit}
          on:change={handleChange}
          disabled={!formData.local_enabled}
        />
        <span>{$t('settings.localAuth.requireNumbers')}</span>
      </label>

      <label class="checkbox-label">
        <input
          type="checkbox"
          bind:checked={formData.password_require_special}
          on:change={handleChange}
          disabled={!formData.local_enabled}
        />
        <span>{$t('settings.localAuth.requireSpecial')}</span>
      </label>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label for="password_max_age_days">{$t('settings.localAuth.passwordExpiry')}</label>
        <input
          id="password_max_age_days"
          type="number"
          bind:value={formData.password_max_age_days}
          on:input={handleChange}
          min="0"
          max="365"
          disabled={!formData.local_enabled}
        />
        <span class="help-text">{$t('settings.localAuth.passwordExpiryHelp')}</span>
      </div>

      <div class="form-group">
        <label for="password_history_count">{$t('settings.localAuth.passwordHistoryCount')}</label>
        <input
          id="password_history_count"
          type="number"
          bind:value={formData.password_history_count}
          on:input={handleChange}
          min="0"
          max="24"
          disabled={!formData.local_enabled}
        />
        <span class="help-text">{$t('settings.localAuth.passwordHistoryHelp')}</span>
      </div>
    </div>
  </div>

  <div class="section" class:disabled={!formData.local_enabled}>
    <h3>{$t('settings.localAuth.mfaTitle')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.mfa_enabled}
        on:change={handleChange}
        disabled={!formData.local_enabled}
      />
      <span>{$t('settings.localAuth.enableTotp')}</span>
    </label>
    <span class="help-text indented">{$t('settings.localAuth.totpHelp')}</span>

    {#if formData.mfa_enabled}
      <div class="mfa-options">
        <label class="checkbox-label">
          <input
            type="checkbox"
            bind:checked={formData.mfa_required}
            on:change={handleChange}
            disabled={!formData.local_enabled}
          />
          <span>{$t('settings.localAuth.requireMfa')}</span>
        </label>
        <span class="help-text indented">{$t('settings.localAuth.requireMfaHelp')}</span>

        <div class="form-group">
          <label for="mfa_issuer_name">{$t('settings.localAuth.mfaIssuerName')}</label>
          <input
            id="mfa_issuer_name"
            type="text"
            bind:value={formData.mfa_issuer_name}
            on:input={handleChange}
            placeholder="OpenTranscribe"
            disabled={!formData.local_enabled}
          />
          <span class="help-text">{$t('settings.localAuth.mfaIssuerHelp')}</span>
        </div>

        <div class="form-row">
          <div class="form-group">
            <label for="mfa_backup_code_count">{$t('settings.localAuth.mfaBackupCodeCount')}</label>
            <input
              id="mfa_backup_code_count"
              type="number"
              bind:value={formData.mfa_backup_code_count}
              on:input={handleChange}
              min="1"
              max="50"
              disabled={!formData.local_enabled}
            />
            <span class="help-text">{$t('settings.localAuth.mfaBackupCodeCountHelp')}</span>
          </div>

          <div class="form-group">
            <label for="mfa_token_expire_minutes">{$t('settings.localAuth.mfaTokenExpiry')}</label>
            <input
              id="mfa_token_expire_minutes"
              type="number"
              bind:value={formData.mfa_token_expire_minutes}
              on:input={handleChange}
              min="1"
              max="60"
              disabled={!formData.local_enabled}
            />
            <span class="help-text">{$t('settings.localAuth.mfaTokenExpiryHelp')}</span>
          </div>
        </div>
      </div>
    {/if}
  </div>

  <div class="section" class:disabled={!formData.local_enabled}>
    <h3>{$t('settings.localAuth.accountLockout')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.account_lockout_enabled}
        on:change={handleChange}
        disabled={!formData.local_enabled}
      />
      <span>{$t('settings.localAuth.lockoutEnabled')}</span>
    </label>
    <span class="help-text indented">{$t('settings.localAuth.lockoutEnabledHelp')}</span>

    {#if formData.account_lockout_enabled}
      <div class="form-row">
        <div class="form-group">
          <label for="account_lockout_threshold">{$t('settings.localAuth.maxLoginAttempts')}</label>
          <input
            id="account_lockout_threshold"
            type="number"
            bind:value={formData.account_lockout_threshold}
            on:input={handleChange}
            min="3"
            max="20"
            disabled={!formData.local_enabled}
          />
          <span class="help-text">{$t('settings.localAuth.maxLoginAttemptsHelp')}</span>
        </div>

        <div class="form-group">
          <label for="account_lockout_duration_minutes">{$t('settings.localAuth.lockoutDuration')}</label>
          <input
            id="account_lockout_duration_minutes"
            type="number"
            bind:value={formData.account_lockout_duration_minutes}
            on:input={handleChange}
            min="1"
            max="1440"
            disabled={!formData.local_enabled}
          />
          <span class="help-text">{$t('settings.localAuth.lockoutDurationHelp')}</span>
        </div>
      </div>

      <label class="checkbox-label">
        <input
          type="checkbox"
          bind:checked={formData.account_lockout_progressive}
          on:change={handleChange}
          disabled={!formData.local_enabled}
        />
        <span>{$t('settings.localAuth.lockoutProgressive')}</span>
      </label>
      <span class="help-text indented">{$t('settings.localAuth.lockoutProgressiveHelp')}</span>

      {#if formData.account_lockout_progressive}
        <div class="form-group">
          <label for="account_lockout_max_duration_minutes">{$t('settings.localAuth.lockoutMaxDuration')}</label>
          <input
            id="account_lockout_max_duration_minutes"
            type="number"
            bind:value={formData.account_lockout_max_duration_minutes}
            on:input={handleChange}
            min="1"
            max="525600"
            disabled={!formData.local_enabled}
          />
          <span class="help-text">{$t('settings.localAuth.lockoutMaxDurationHelp')}</span>
        </div>
      {/if}
    {/if}
  </div>

  <!--
    Not gated on formData.local_enabled: the banner is enforced for every user
    regardless of how they authenticate (FedRAMP AC-8), so it stays interactive
    even on an SSO-only deployment.
  -->
  <div class="section">
    <h3>{$t('settings.localAuth.loginBannerTitle')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.login_banner_enabled}
        on:change={handleChange}
      />
      <span>{$t('settings.localAuth.loginBannerEnable')}</span>
    </label>
    <span class="help-text indented">{$t('settings.localAuth.loginBannerEnableHelp')}</span>

    {#if formData.login_banner_enabled}
      <div class="form-group">
        <label for="login_banner_classification">{$t('settings.localAuth.loginBannerClassification')}</label>
        <input
          id="login_banner_classification"
          type="text"
          bind:value={formData.login_banner_classification}
          on:input={handleChange}
          placeholder="UNCLASSIFIED"
        />
      </div>

      <div class="form-group">
        <label for="login_banner_text">{$t('settings.localAuth.loginBannerText')}</label>
        <textarea
          id="login_banner_text"
          bind:value={formData.login_banner_text}
          on:input={handleChange}
          rows="6"
          maxlength="10000"
        ></textarea>
        <span class="help-text">{$t('settings.localAuth.loginBannerTextHelp')}</span>
      </div>
    {/if}
  </div>

  <div class="actions">
    <button
      class="btn btn-primary"
      on:click={handleSave}
      disabled={saving}
    >
      {saving ? $t('common.saving') : $t('settings.localAuth.saveConfiguration')}
    </button>
  </div>
</div>

<style>
  .settings-panel {
    max-width: 800px;
  }

  .enable-toggle {
    margin-bottom: 1.5rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid var(--color-border);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    cursor: pointer;
  }

  .toggle-label input[type="checkbox"] {
    width: 1.25rem;
    height: 1.25rem;
    cursor: pointer;
  }

  .toggle-text {
    font-weight: 500;
    font-size: 1rem;
  }

  .warning-banner {
    margin: 0 0 1.5rem 0;
    padding: 0.75rem 1rem;
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-radius: 6px;
    background: rgba(245, 158, 11, 0.12);
    color: var(--color-text);
    font-size: 0.8125rem;
    line-height: 1.45;
  }

  .section {
    margin-bottom: 2rem;
    padding: 1rem;
    background: var(--color-bg-secondary);
    border-radius: 8px;
    transition: opacity 0.2s;
  }

  .section.disabled {
    opacity: 0.5;
    pointer-events: none;
  }

  .section h3 {
    margin: 0 0 1rem 0;
    font-size: 0.875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--color-text-secondary);
  }

  .info-note {
    margin: 0.5rem 0 0 1.5rem;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--color-info-border, rgba(var(--primary-color-rgb), 0.3));
    border-radius: 6px;
    background: var(--color-info-bg, rgba(var(--primary-color-rgb), 0.08));
    color: var(--color-text-secondary);
    font-size: 0.75rem;
    line-height: 1.5;
  }

  .policy-preview {
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 4px;
    padding: 0.75rem;
    margin-bottom: 1rem;
    font-size: 0.875rem;
  }

  .policy-preview strong {
    color: var(--color-text);
  }

  .form-row {
    display: flex;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .form-group {
    flex: 1;
    margin-bottom: 1rem;
  }

  .form-group label {
    display: block;
    margin-bottom: 0.5rem;
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--color-text);
  }

  .form-group input[type="text"],
  .form-group input[type="number"],
  .form-group textarea {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 0.875rem;
    font-family: inherit;
    resize: vertical;
  }

  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px var(--color-primary-alpha);
  }

  .form-group input:disabled {
    background: var(--color-bg-tertiary);
    cursor: not-allowed;
  }

  .help-text {
    display: block;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
  }

  .help-text.indented {
    margin-left: 1.5rem;
    margin-bottom: 0.75rem;
  }

  .checkbox-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0.5rem;
    margin-bottom: 1rem;
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-size: 0.875rem;
    margin-bottom: 0.5rem;
  }

  .checkbox-label input[type="checkbox"] {
    width: 1rem;
    height: 1rem;
    cursor: pointer;
  }

  .checkbox-label input:disabled {
    cursor: not-allowed;
  }

  .mfa-options {
    margin-top: 1rem;
    padding-left: 1.5rem;
  }

  .actions {
    display: flex;
    gap: 1rem;
    justify-content: flex-end;
    padding-top: 1rem;
    border-top: 1px solid var(--color-border);
  }

  @media (max-width: 768px) {
    .form-row {
      flex-direction: column;
      gap: 0;
    }

    .checkbox-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
