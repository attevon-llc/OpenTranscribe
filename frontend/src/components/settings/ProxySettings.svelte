<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { ProxyConfig } from '$lib/api/authConfig';

  export let config: Partial<ProxyConfig> = {};

  /**
   * Whether the backend reports a stored shared secret (`is_set` on the
   * sensitive config row). The secret itself never reaches the browser, so
   * this is the only signal available for the "leave blank to keep the
   * current value" affordance. Falls back to `config.proxy_shared_secret ===
   * null`, which is what a stored sensitive row flattens to.
   */
  export let sharedSecretIsSet: boolean | undefined = undefined;

  const dispatch = createEventDispatcher();

  function buildFormData(source: Partial<ProxyConfig>): Omit<ProxyConfig, 'proxy_shared_secret'> {
    return {
      proxy_enabled: source.proxy_enabled ?? false,
      proxy_trusted_proxies: source.proxy_trusted_proxies ?? '',
      proxy_email_header: source.proxy_email_header ?? 'X-Forwarded-Email',
      proxy_name_header: source.proxy_name_header ?? 'X-Forwarded-User',
      proxy_groups_header: source.proxy_groups_header ?? '',
      proxy_groups_separator: source.proxy_groups_separator ?? ',',
      proxy_role_header: source.proxy_role_header ?? '',
      proxy_allowed_domains: source.proxy_allowed_domains ?? '',
      proxy_jit_provisioning: source.proxy_jit_provisioning ?? true
    };
  }

  let formData = buildFormData(config);

  /**
   * Held outside `formData` so a secret is only ever submitted when the
   * admin typed one. Never bind the API's placeholder into the field — the
   * next save would encrypt that placeholder over the real secret.
   */
  let sharedSecretInput = '';

  let saving = false;
  let showSecret = false;

  $: if (config) {
    formData = buildFormData(config);
    // A reload means the previous edit either landed or was discarded; the
    // typed secret must not survive into the next save either way.
    sharedSecretInput = '';
  }

  $: sharedSecretSet = sharedSecretIsSet ?? config.proxy_shared_secret === null;

  /** Empty admits every proxy-asserted role — stated permanently, like PKI/OIDC. */
  $: roleHeaderOff = (formData.proxy_role_header ?? '').trim() === '';

  function buildPayload(): Record<string, unknown> {
    const payload: Record<string, unknown> = { ...formData };
    if (sharedSecretInput !== '') {
      payload.proxy_shared_secret = sharedSecretInput;
    }
    return payload;
  }

  function handleChange() {
    dispatch('change');
  }

  function handleSave() {
    saving = true;
    dispatch('save', buildPayload());
    setTimeout(() => saving = false, 500);
  }
</script>

<div class="settings-panel">
  <div class="enable-toggle">
    <label class="toggle-label">
      <input
        type="checkbox"
        bind:checked={formData.proxy_enabled}
        on:change={handleChange}
      />
      <span class="toggle-text">{$t('settings.proxy.enable')}</span>
    </label>
  </div>

  <div class="warning-box">
    <strong>{$t('settings.proxy.advancedConfiguration')}</strong>
    <p>{$t('settings.proxy.advancedConfigurationDesc')}</p>
  </div>

  <div class="section" class:disabled={!formData.proxy_enabled}>
    <h3>{$t('settings.proxy.trustBoundary')}</h3>

    <div class="form-group">
      <label for="proxy_trusted_proxies">{$t('settings.proxy.trustedProxies')}</label>
      <input
        id="proxy_trusted_proxies"
        type="text"
        bind:value={formData.proxy_trusted_proxies}
        on:input={handleChange}
        placeholder="e.g. 203.0.113.5/32 (your proxy's IP) or its docker subnet — never a whole private range"
        disabled={!formData.proxy_enabled}
      />
      <span class="help-text">{$t('settings.proxy.trustedProxiesHelp')}</span>
    </div>

    <div class="form-group">
      <label for="proxy_shared_secret">{$t('settings.proxy.sharedSecret')}</label>
      <div class="input-with-toggle">
        <input
          id="proxy_shared_secret"
          type={showSecret ? 'text' : 'password'}
          bind:value={sharedSecretInput}
          on:input={handleChange}
          autocomplete="new-password"
          placeholder={sharedSecretSet
            ? $t('settings.proxy.sharedSecretKeepPlaceholder')
            : $t('settings.proxy.enterSharedSecret')}
          disabled={!formData.proxy_enabled}
        />
        <button
          type="button"
          class="toggle-visibility"
          on:click={() => showSecret = !showSecret}
          disabled={!formData.proxy_enabled}
        >
          {showSecret ? $t('common.hide') : $t('common.show')}
        </button>
      </div>
      <span class="help-text">
        {sharedSecretSet
          ? $t('settings.proxy.sharedSecretSetHelp')
          : $t('settings.proxy.sharedSecretHelp')}
      </span>
    </div>
  </div>

  <div class="section" class:disabled={!formData.proxy_enabled}>
    <h3>{$t('settings.proxy.headerConfiguration')}</h3>

    <div class="form-row">
      <div class="form-group">
        <label for="proxy_email_header">{$t('settings.proxy.emailHeader')}</label>
        <input
          id="proxy_email_header"
          type="text"
          bind:value={formData.proxy_email_header}
          on:input={handleChange}
          placeholder="X-Forwarded-Email"
          disabled={!formData.proxy_enabled}
        />
        <span class="help-text">{$t('settings.proxy.emailHeaderHelp')}</span>
      </div>

      <div class="form-group">
        <label for="proxy_name_header">{$t('settings.proxy.nameHeader')}</label>
        <input
          id="proxy_name_header"
          type="text"
          bind:value={formData.proxy_name_header}
          on:input={handleChange}
          placeholder="X-Forwarded-User"
          disabled={!formData.proxy_enabled}
        />
        <span class="help-text">{$t('settings.proxy.nameHeaderHelp')}</span>
      </div>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label for="proxy_groups_header">{$t('settings.proxy.groupsHeader')}</label>
        <input
          id="proxy_groups_header"
          type="text"
          bind:value={formData.proxy_groups_header}
          on:input={handleChange}
          placeholder="X-Forwarded-Groups"
          disabled={!formData.proxy_enabled}
        />
        <span class="help-text">{$t('settings.proxy.groupsHeaderHelp')}</span>
      </div>

      <div class="form-group">
        <label for="proxy_groups_separator">{$t('settings.proxy.groupsSeparator')}</label>
        <input
          id="proxy_groups_separator"
          type="text"
          bind:value={formData.proxy_groups_separator}
          on:input={handleChange}
          placeholder=","
          disabled={!formData.proxy_enabled}
        />
        <span class="help-text">{$t('settings.proxy.groupsSeparatorHelp')}</span>
      </div>
    </div>
  </div>

  <div class="section" class:disabled={!formData.proxy_enabled}>
    <h3>{$t('settings.proxy.authorization')}</h3>

    <div class="form-group">
      <label for="proxy_role_header">{$t('settings.proxy.roleHeader')}</label>
      <input
        id="proxy_role_header"
        type="text"
        bind:value={formData.proxy_role_header}
        on:input={handleChange}
        placeholder="X-Forwarded-Role"
        disabled={!formData.proxy_enabled}
      />
      <span class="help-text">{$t('settings.proxy.roleHeaderHelp')}</span>
    </div>

    {#if roleHeaderOff}
      <p class="ceiling-warning" role="status">
        {$t('settings.proxy.roleHeaderOffNotice')}
      </p>
    {/if}

    <div class="form-group">
      <label for="proxy_allowed_domains">{$t('settings.proxy.allowedDomains')}</label>
      <input
        id="proxy_allowed_domains"
        type="text"
        bind:value={formData.proxy_allowed_domains}
        on:input={handleChange}
        placeholder="example.com, example.org"
        disabled={!formData.proxy_enabled}
      />
      <span class="help-text">{$t('settings.proxy.allowedDomainsHelp')}</span>
    </div>

    <label class="checkbox-label">
      <input
        type="checkbox"
        bind:checked={formData.proxy_jit_provisioning}
        on:change={handleChange}
        disabled={!formData.proxy_enabled}
      />
      <span>{$t('settings.proxy.jitProvisioning')}</span>
    </label>
    <span class="help-text indented">{$t('settings.proxy.jitProvisioningHelp')}</span>
  </div>

  <div class="actions">
    <button
      class="btn btn-primary"
      on:click={handleSave}
      disabled={saving}
    >
      {saving ? $t('common.saving') : $t('settings.proxy.saveConfiguration')}
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
  .form-group input[type="password"] {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 0.875rem;
    font-family: inherit;
  }

  .form-group input:focus {
    outline: none;
    border-color: var(--color-primary);
    box-shadow: 0 0 0 2px var(--color-primary-alpha);
  }

  .form-group input:disabled {
    background: var(--color-bg-tertiary);
    cursor: not-allowed;
  }

  .input-with-toggle {
    display: flex;
    gap: 0.5rem;
  }

  .input-with-toggle input {
    flex: 1;
  }

  .toggle-visibility {
    padding: 0.5rem 0.75rem;
    background: var(--color-bg-tertiary);
    border: 1px solid var(--color-border);
    border-radius: 4px;
    color: var(--color-text-secondary);
    font-size: 0.75rem;
    cursor: pointer;
    white-space: nowrap;
  }

  .toggle-visibility:hover:not(:disabled) {
    background: var(--color-bg-hover);
  }

  .toggle-visibility:disabled {
    cursor: not-allowed;
    opacity: 0.6;
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
    margin: 0.5rem 0 1rem 0;
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
