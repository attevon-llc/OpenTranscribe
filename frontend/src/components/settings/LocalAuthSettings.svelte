<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import PasswordPolicySettings from './PasswordPolicySettings.svelte';
  import type { AuthConfigValues } from '$lib/api/authConfig';
  import { normalizeTier } from '$lib/passwordPolicyTiers';

  // All fields come from a single config object (saved under "local" category)
  export let config: AuthConfigValues = {};
  // Bumped by the parent after a save so the panel re-reads the policy the server enforces.
  export let refreshKey = 0;

  const dispatch = createEventDispatcher();

  // Helper to get value with default - only use default if value is truly undefined
  function getVal<T>(key: string, defaultVal: T): T {
    const val = config[key];
    return val !== undefined ? (val as T) : defaultVal;
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
      // Coded default for an install that never chose a tier: hardened (the DoD-style
      // rules above), so the form never invents a policy the server is not enforcing.
      password_policy_profile: normalizeTier(getVal('password_policy_profile', 'hardened')),
      password_max_length: getVal('password_max_length', 0),
      password_min_age_hours: getVal('password_min_age_hours', 24),
      // '' = the tier's own default; 'true' / 'false' override it.
      password_blocklist_enabled: getVal('password_blocklist_enabled', ''),
      password_hibp_enabled: getVal('password_hibp_enabled', false),
      // MFA
      mfa_enabled: getVal('mfa_enabled', false),
      mfa_required: getVal('mfa_required', false),
      mfa_required_for_admins: getVal('mfa_required_for_admins', false),
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
    <PasswordPolicySettings
      bind:data={formData}
      disabled={!formData.local_enabled}
      {refreshKey}
      on:change={handleChange}
    />
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

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.mfa_required_for_admins}
        on:change={handleChange}
        disabled={!formData.local_enabled}
      />
      <span>{$t('settings.localAuth.requireMfaAdmins')}</span>
    </label>
    <span class="help-text indented">{$t('settings.localAuth.requireMfaAdminsHelp')}</span>

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

        <FormField size="lg" id="mfa_issuer_name" label={$t('settings.localAuth.mfaIssuerName')} hint={$t('settings.localAuth.mfaIssuerHelp')} let:describedby>
          <input
            id="mfa_issuer_name" aria-describedby={describedby}
            type="text"
            bind:value={formData.mfa_issuer_name}
            on:input={handleChange}
            placeholder="OpenTranscribe"
            disabled={!formData.local_enabled}
          />
        </FormField>

        <div class="form-row">
          <FormField size="lg" id="mfa_backup_code_count" label={$t('settings.localAuth.mfaBackupCodeCount')} hint={$t('settings.localAuth.mfaBackupCodeCountHelp')} let:describedby>
            <input
              id="mfa_backup_code_count" aria-describedby={describedby}
              type="number"
              bind:value={formData.mfa_backup_code_count}
              on:input={handleChange}
              min="1"
              max="50"
              disabled={!formData.local_enabled}
            />
          </FormField>

          <FormField size="lg" id="mfa_token_expire_minutes" label={$t('settings.localAuth.mfaTokenExpiry')} hint={$t('settings.localAuth.mfaTokenExpiryHelp')} let:describedby>
            <input
              id="mfa_token_expire_minutes" aria-describedby={describedby}
              type="number"
              bind:value={formData.mfa_token_expire_minutes}
              on:input={handleChange}
              min="1"
              max="60"
              disabled={!formData.local_enabled}
            />
          </FormField>
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
        <FormField size="lg" id="account_lockout_threshold" label={$t('settings.localAuth.maxLoginAttempts')} hint={$t('settings.localAuth.maxLoginAttemptsHelp')} let:describedby>
          <input
            id="account_lockout_threshold" aria-describedby={describedby}
            type="number"
            bind:value={formData.account_lockout_threshold}
            on:input={handleChange}
            min="3"
            max="20"
            disabled={!formData.local_enabled}
          />
        </FormField>

        <FormField size="lg" id="account_lockout_duration_minutes" label={$t('settings.localAuth.lockoutDuration')} hint={$t('settings.localAuth.lockoutDurationHelp')} let:describedby>
          <input
            id="account_lockout_duration_minutes" aria-describedby={describedby}
            type="number"
            bind:value={formData.account_lockout_duration_minutes}
            on:input={handleChange}
            min="1"
            max="1440"
            disabled={!formData.local_enabled}
          />
        </FormField>
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
        <FormField size="lg" id="account_lockout_max_duration_minutes" label={$t('settings.localAuth.lockoutMaxDuration')} hint={$t('settings.localAuth.lockoutMaxDurationHelp')} let:describedby>
          <input
            id="account_lockout_max_duration_minutes" aria-describedby={describedby}
            type="number"
            bind:value={formData.account_lockout_max_duration_minutes}
            on:input={handleChange}
            min="1"
            max="525600"
            disabled={!formData.local_enabled}
          />
        </FormField>
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
      <FormField size="lg" id="login_banner_classification" label={$t('settings.localAuth.loginBannerClassification')}>
        <input
          id="login_banner_classification"
          type="text"
          bind:value={formData.login_banner_classification}
          on:input={handleChange}
          placeholder="UNCLASSIFIED"
        />
      </FormField>

      <FormField size="lg" id="login_banner_text" label={$t('settings.localAuth.loginBannerText')} hint={$t('settings.localAuth.loginBannerTextHelp')} let:describedby>
        <textarea
          id="login_banner_text" aria-describedby={describedby}
          bind:value={formData.login_banner_text}
          on:input={handleChange}
          rows="6"
          maxlength="10000"
        ></textarea>
      </FormField>
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
    margin-bottom: var(--space-6);
    padding-bottom: var(--space-4);
    border-bottom: 1px solid var(--color-border);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    cursor: pointer;
  }

  .toggle-label input[type="checkbox"] {
    width: 1.25rem;
    height: 1.25rem;
    cursor: pointer;
  }

  .toggle-text {
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-base);
  }

  .warning-banner {
    margin: 0 0 var(--space-6) 0;
    padding: var(--space-3) var(--space-4);
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-radius: var(--radius-md);
    background: rgba(245, 158, 11, 0.12);
    color: var(--color-text);
    font-size: var(--font-size-sm);
    line-height: 1.45;
  }

  .section {
    margin-bottom: var(--space-8);
    padding: var(--space-4);
    background: var(--color-bg-secondary);
    border-radius: var(--radius-lg);
    transition: opacity var(--duration-base);
  }

  .section.disabled {
    opacity: 0.5;
    pointer-events: none;
  }

  .section h3 {
    margin: 0 0 var(--space-4) 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--color-text-secondary);
  }

  .info-note {
    margin: var(--space-2) 0 0 var(--space-6);
    padding: 0.6rem var(--space-3);
    border: 1px solid var(--color-info-border);
    border-radius: var(--radius-md);
    background: var(--color-info-bg);
    color: var(--color-text-secondary);
    font-size: var(--font-size-xs);
    line-height: 1.5;
  }

  .form-row {
    display: flex;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
  }

  .section :global(.form-field) {
    flex: 1;
    margin-bottom: var(--space-4);
  }

  .section :global(.form-field input[type="text"]),
  .section :global(.form-field input[type="number"]),
  .section :global(.form-field textarea) {
    width: 100%;
    resize: vertical;
  }

  .section :global(.form-field input:disabled) {
    cursor: not-allowed;
  }

  .help-text {
    display: block;
    margin-top: var(--space-1);
    font-size: var(--font-size-xs);
    color: var(--color-text-tertiary);
  }

  .help-text.indented {
    margin-left: var(--space-6);
    margin-bottom: var(--space-3);
  }


  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    font-size: var(--font-size-md);
    margin-bottom: var(--space-2);
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
    margin-top: var(--space-4);
    padding-left: var(--space-6);
  }

  .actions {
    display: flex;
    gap: var(--space-4);
    justify-content: flex-end;
    padding-top: var(--space-4);
    border-top: 1px solid var(--color-border);
  }

  @media (max-width: 768px) {
    .form-row {
      flex-direction: column;
      gap: 0;
    }

  }
</style>
