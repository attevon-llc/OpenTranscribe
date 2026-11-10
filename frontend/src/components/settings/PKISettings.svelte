<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { PKIConfig } from '$lib/api/authConfig';

  export let config: Partial<PKIConfig> = {};

  const dispatch = createEventDispatcher();

  let formData: PKIConfig = {
    pki_enabled: config.pki_enabled ?? false,
    pki_ca_cert_path: config.pki_ca_cert_path ?? '',
    pki_verify_revocation: config.pki_verify_revocation ?? false,
    pki_cert_header: config.pki_cert_header ?? 'X-SSL-Client-Cert',
    pki_cert_dn_header: config.pki_cert_dn_header ?? 'X-SSL-Client-DN',
    pki_admin_dns: config.pki_admin_dns ?? '',
    pki_ocsp_timeout_seconds: config.pki_ocsp_timeout_seconds ?? 10,
    pki_crl_cache_seconds: config.pki_crl_cache_seconds ?? 3600,
    pki_revocation_soft_fail: config.pki_revocation_soft_fail ?? true,
    pki_trusted_proxies: config.pki_trusted_proxies ?? '',
    pki_mode: config.pki_mode ?? 'header',
    // MUST default true, matching `PKIConfig` in backend/app/schemas/auth_config.py.
    // This flag is a deployment-level CEILING: effective fallback is
    // `user.allow_local_fallback AND pki_allow_password_fallback`. A `true`
    // ceiling restricts nothing (the per-user flag already defaults to false),
    // while a `false` one revokes fallback from every account a super_admin
    // deliberately granted it to — which is what saving this panel on an
    // unconfigured deployment used to do.
    pki_allow_password_fallback: config.pki_allow_password_fallback ?? true
  };

  let saving = false;

  $: if (config) {
    formData = {
      pki_enabled: config.pki_enabled ?? false,
      pki_ca_cert_path: config.pki_ca_cert_path ?? '',
      pki_verify_revocation: config.pki_verify_revocation ?? false,
      pki_cert_header: config.pki_cert_header ?? 'X-SSL-Client-Cert',
      pki_cert_dn_header: config.pki_cert_dn_header ?? 'X-SSL-Client-DN',
      pki_admin_dns: config.pki_admin_dns ?? '',
      pki_ocsp_timeout_seconds: config.pki_ocsp_timeout_seconds ?? 10,
      pki_crl_cache_seconds: config.pki_crl_cache_seconds ?? 3600,
      pki_revocation_soft_fail: config.pki_revocation_soft_fail ?? true,
      pki_trusted_proxies: config.pki_trusted_proxies ?? '',
      pki_mode: config.pki_mode ?? 'header',
      // Ceiling, not a grant — see the comment on the initial value above.
      pki_allow_password_fallback: config.pki_allow_password_fallback ?? true
    };
  }

  function handleChange() {
    dispatch('change');
  }

  function handleSave() {
    saving = true;
    dispatch('save', formData);
    setTimeout(() => saving = false, 500);
  }

  const pkiModes = [
    { value: 'header', labelKey: 'settings.pki.modeHeader', descKey: 'settings.pki.modeHeaderDesc' },
    { value: 'mutual_tls', labelKey: 'settings.pki.modeMutualTls', descKey: 'settings.pki.modeMutualTlsDesc' }
  ];
</script>

<div class="settings-panel auth-panel">
  <div class="enable-toggle">
    <label class="toggle-label">
      <input
        type="checkbox"
        bind:checked={formData.pki_enabled}
        on:change={handleChange}
      />
      <span class="toggle-text">{$t('settings.pki.enable')}</span>
    </label>
  </div>

  <div class="warning-box">
    <strong>{$t('settings.pki.advancedConfiguration')}</strong>
    <p>{$t('settings.pki.advancedConfigurationDesc')}</p>
  </div>

  <div class="section" class:disabled={!formData.pki_enabled}>
    <h3>{$t('settings.pki.authenticationMode')}</h3>

    <div class="radio-group">
      {#each pkiModes as mode (mode.value)}
        <label class="radio-label">
          <input
            type="radio"
            name="pki_mode"
            value={mode.value}
            bind:group={formData.pki_mode}
            on:change={handleChange}
            disabled={!formData.pki_enabled}
          />
          <div class="radio-content">
            <span class="radio-title">{$t(mode.labelKey)}</span>
            <span class="radio-description">{$t(mode.descKey)}</span>
          </div>
        </label>
      {/each}
    </div>
  </div>

  <div class="section" class:disabled={!formData.pki_enabled}>
    <h3>{$t('settings.pki.certificateAuthority')}</h3>

    <FormField size="lg" id="pki_ca_cert_path" label={$t('settings.pki.caCertPath')} hint={$t('settings.pki.caCertPathHelp')} let:describedby>
      <input dir="ltr"
        id="pki_ca_cert_path" aria-describedby={describedby}
        type="text"
        bind:value={formData.pki_ca_cert_path}
        on:input={handleChange}
        placeholder="/etc/ssl/certs/ca-certificates.crt"
        disabled={!formData.pki_enabled}
      />
    </FormField>
  </div>

  {#if formData.pki_mode === 'header'}
    <div class="section" class:disabled={!formData.pki_enabled}>
      <h3>{$t('settings.pki.headerConfiguration')}</h3>

      <div class="form-row">
        <FormField size="lg" id="pki_cert_header" label={$t('settings.pki.certHeader')} hint={$t('settings.pki.certHeaderHelp')} let:describedby>
          <input dir="ltr"
            id="pki_cert_header" aria-describedby={describedby}
            type="text"
            bind:value={formData.pki_cert_header}
            on:input={handleChange}
            placeholder="X-SSL-Client-Cert"
            disabled={!formData.pki_enabled}
          />
        </FormField>

        <FormField size="lg" id="pki_cert_dn_header" label={$t('settings.pki.dnHeader')} hint={$t('settings.pki.dnHeaderHelp')} let:describedby>
          <input dir="ltr"
            id="pki_cert_dn_header" aria-describedby={describedby}
            type="text"
            bind:value={formData.pki_cert_dn_header}
            on:input={handleChange}
            placeholder="X-SSL-Client-DN"
            disabled={!formData.pki_enabled}
          />
        </FormField>
      </div>

      <FormField size="lg" id="pki_trusted_proxies" label={$t('settings.pki.trustedProxies')} hint={$t('settings.pki.trustedProxiesHelp')} let:describedby>
        <input dir="ltr"
          id="pki_trusted_proxies" aria-describedby={describedby}
          type="text"
          bind:value={formData.pki_trusted_proxies}
          on:input={handleChange}
          placeholder="e.g. 203.0.113.5/32 (your proxy's IP) or its docker subnet — never a whole private range"
          disabled={!formData.pki_enabled}
        />
      </FormField>
    </div>
  {/if}

  <div class="section" class:disabled={!formData.pki_enabled}>
    <h3>{$t('settings.pki.revocationChecking')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.pki_verify_revocation}
        on:change={handleChange}
        disabled={!formData.pki_enabled}
      />
      <span>{$t('settings.pki.enableRevocationChecking')}</span>
    </label>

    {#if formData.pki_verify_revocation}
      <div class="revocation-options">
        <div class="form-row">
          <FormField size="lg" id="pki_ocsp_timeout_seconds" label={$t('settings.pki.ocspTimeout')}>
            <input
              id="pki_ocsp_timeout_seconds"
              type="number"
              bind:value={formData.pki_ocsp_timeout_seconds}
              on:input={handleChange}
              min="1"
              max="60"
              disabled={!formData.pki_enabled}
            />
          </FormField>

          <FormField size="lg" id="pki_crl_cache_seconds" label={$t('settings.pki.crlCacheDuration')}>
            <input
              id="pki_crl_cache_seconds"
              type="number"
              bind:value={formData.pki_crl_cache_seconds}
              on:input={handleChange}
              min="60"
              max="86400"
              disabled={!formData.pki_enabled}
            />
          </FormField>
        </div>

        <label class="checkbox-label">
          <input
            type="checkbox"
            bind:checked={formData.pki_revocation_soft_fail}
            on:change={handleChange}
            disabled={!formData.pki_enabled}
          />
          <span>{$t('settings.pki.softFail')}</span>
        </label>
        <span class="help-text indented">{$t('settings.pki.softFailHelp')}</span>
      </div>
    {/if}
  </div>

  <div class="section" class:disabled={!formData.pki_enabled}>
    <h3>{$t('settings.pki.authorization')}</h3>

    <FormField size="lg" id="pki_admin_dns" label={$t('settings.pki.adminDns')} hint={$t('settings.pki.adminDnsHelp')} let:describedby>
      <textarea dir="ltr"
        id="pki_admin_dns" aria-describedby={describedby}
        bind:value={formData.pki_admin_dns}
        on:input={handleChange}
        placeholder="CN=Admin User,OU=Admins,O=Example Corp,C=US"
        rows="3"
        disabled={!formData.pki_enabled}
      ></textarea>
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.pki_enabled}>
    <h3>{$t('settings.pki.fallbackOptions')}</h3>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.pki_allow_password_fallback}
        on:change={handleChange}
        disabled={!formData.pki_enabled}
      />
      <span>{$t('settings.pki.allowPasswordFallback')}</span>
    </label>
    <span class="help-text indented">{$t('settings.pki.allowPasswordFallbackHelp')}</span>

    {#if !formData.pki_allow_password_fallback}
      <!-- Clearing a ceiling is subtractive and silent: it revokes fallback from
           accounts a super_admin granted it to, one by one, with no other signal. -->
      <p class="ceiling-warning" role="status">
        {$t('settings.pki.allowPasswordFallbackWarning')}
      </p>
    {/if}
  </div>

  <div class="actions">
    <button
      class="btn btn-primary"
      on:click={handleSave}
      disabled={saving}
    >
      {saving ? $t('common.saving') : $t('settings.pki.saveConfiguration')}
    </button>
  </div>
</div>

<style>

  .radio-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .radio-label {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    padding: var(--space-3);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-lg);
    cursor: pointer;
    transition: border-color var(--duration-base), background-color var(--duration-base);
  }

  .radio-label:hover {
    border-color: var(--color-primary);
  }

  .radio-label input[type="radio"] {
    margin-top: var(--space-1);
    width: 1rem;
    height: 1rem;
  }

  .radio-content {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .radio-title {
    font-weight: var(--font-weight-medium);
    color: var(--color-text);
  }

  .radio-description {
    font-size: var(--font-size-xs);
    color: var(--color-text-tertiary);
  }

  .section :global(.form-field) {
    flex: 1;
    margin-bottom: var(--space-4);
  }

  .section :global(.form-field input[type="text"]),
  .section :global(.form-field input[type="number"]),
  .section :global(.form-field textarea) {
    width: 100%;
  }

  .section :global(.form-field textarea) {
    resize: vertical;
    min-height: 80px;
  }

  .section :global(.form-field input:disabled),
  .section :global(.form-field textarea:disabled) {
    cursor: not-allowed;
  }

  .help-text.indented {
    margin-left: var(--space-6);
    margin-bottom: var(--space-3);
  }

  .ceiling-warning {
    margin: var(--space-2) 0 0 var(--space-6);
    padding: var(--space-2-5) var(--space-3);
    border: 1px solid var(--color-warning-border);
    border-radius: var(--radius-md);
    background: var(--color-warning-bg);
    color: var(--color-warning-text);
    font-size: var(--font-size-xs);
  }

  .checkbox-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    font-size: var(--font-size-md);
    margin-bottom: var(--space-2);
  }

  .revocation-options {
    margin-top: var(--space-4);
    padding-left: var(--space-6);
  }

  @media (max-width: 768px) {
    .form-row {
      flex-direction: column;
      gap: 0;
    }
  }
</style>
