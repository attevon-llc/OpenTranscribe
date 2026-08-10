<script lang="ts">
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

<div class="settings-panel">
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
      {#each pkiModes as mode}
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

    <div class="form-group">
      <label for="pki_ca_cert_path">{$t('settings.pki.caCertPath')}</label>
      <input
        id="pki_ca_cert_path"
        type="text"
        bind:value={formData.pki_ca_cert_path}
        on:input={handleChange}
        placeholder="/etc/ssl/certs/ca-certificates.crt"
        disabled={!formData.pki_enabled}
      />
      <span class="help-text">{$t('settings.pki.caCertPathHelp')}</span>
    </div>
  </div>

  {#if formData.pki_mode === 'header'}
    <div class="section" class:disabled={!formData.pki_enabled}>
      <h3>{$t('settings.pki.headerConfiguration')}</h3>

      <div class="form-row">
        <div class="form-group">
          <label for="pki_cert_header">{$t('settings.pki.certHeader')}</label>
          <input
            id="pki_cert_header"
            type="text"
            bind:value={formData.pki_cert_header}
            on:input={handleChange}
            placeholder="X-SSL-Client-Cert"
            disabled={!formData.pki_enabled}
          />
          <span class="help-text">{$t('settings.pki.certHeaderHelp')}</span>
        </div>

        <div class="form-group">
          <label for="pki_cert_dn_header">{$t('settings.pki.dnHeader')}</label>
          <input
            id="pki_cert_dn_header"
            type="text"
            bind:value={formData.pki_cert_dn_header}
            on:input={handleChange}
            placeholder="X-SSL-Client-DN"
            disabled={!formData.pki_enabled}
          />
          <span class="help-text">{$t('settings.pki.dnHeaderHelp')}</span>
        </div>
      </div>

      <div class="form-group">
        <label for="pki_trusted_proxies">{$t('settings.pki.trustedProxies')}</label>
        <input
          id="pki_trusted_proxies"
          type="text"
          bind:value={formData.pki_trusted_proxies}
          on:input={handleChange}
          placeholder="10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16"
          disabled={!formData.pki_enabled}
        />
        <span class="help-text">{$t('settings.pki.trustedProxiesHelp')}</span>
      </div>
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
          <div class="form-group">
            <label for="pki_ocsp_timeout_seconds">{$t('settings.pki.ocspTimeout')}</label>
            <input
              id="pki_ocsp_timeout_seconds"
              type="number"
              bind:value={formData.pki_ocsp_timeout_seconds}
              on:input={handleChange}
              min="1"
              max="60"
              disabled={!formData.pki_enabled}
            />
          </div>

          <div class="form-group">
            <label for="pki_crl_cache_seconds">{$t('settings.pki.crlCacheDuration')}</label>
            <input
              id="pki_crl_cache_seconds"
              type="number"
              bind:value={formData.pki_crl_cache_seconds}
              on:input={handleChange}
              min="60"
              max="86400"
              disabled={!formData.pki_enabled}
            />
          </div>
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

    <div class="form-group">
      <label for="pki_admin_dns">{$t('settings.pki.adminDns')}</label>
      <textarea
        id="pki_admin_dns"
        bind:value={formData.pki_admin_dns}
        on:input={handleChange}
        placeholder="CN=Admin User,OU=Admins,O=Example Corp,C=US"
        rows="3"
        disabled={!formData.pki_enabled}
      ></textarea>
      <span class="help-text">{$t('settings.pki.adminDnsHelp')}</span>
    </div>
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

  .warning-box {
    background: var(--color-warning-bg);
    border: 1px solid var(--color-warning-border);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 1.5rem;
  }

  .warning-box strong {
    color: var(--color-warning-text);
  }

  .warning-box p {
    margin: 0.5rem 0 0 0;
    font-size: 0.875rem;
    color: var(--color-text-secondary);
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

  .radio-group {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .radio-label {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    padding: 0.75rem;
    border: 1px solid var(--color-border);
    border-radius: 8px;
    cursor: pointer;
    transition: border-color 0.2s, background-color 0.2s;
  }

  .radio-label:hover {
    border-color: var(--color-primary);
  }

  .radio-label input[type="radio"] {
    margin-top: 0.25rem;
    width: 1rem;
    height: 1rem;
  }

  .radio-content {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .radio-title {
    font-weight: 500;
    color: var(--color-text);
  }

  .radio-description {
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
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
  }

  .form-group textarea {
    resize: vertical;
    min-height: 80px;
  }

  .form-group input:focus,
  .form-group textarea:focus {
    outline: none;
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px var(--color-primary-alpha);
  }

  .form-group input:disabled,
  .form-group textarea:disabled {
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

  .ceiling-warning {
    margin: 0.5rem 0 0 1.5rem;
    padding: 0.625rem 0.75rem;
    border: 1px solid var(--color-warning-border);
    border-radius: 6px;
    background: var(--color-warning-bg);
    color: var(--color-warning-text);
    font-size: 0.75rem;
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

  .revocation-options {
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
  }
</style>
