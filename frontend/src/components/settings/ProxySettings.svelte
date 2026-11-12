<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
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

<div class="settings-panel auth-panel">
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

    <FormField size="lg" id="proxy_trusted_proxies" label={$t('settings.proxy.trustedProxies')} hint={$t('settings.proxy.trustedProxiesHelp')} let:describedby>
      <input dir="ltr"
        id="proxy_trusted_proxies" aria-describedby={describedby}
        type="text"
        bind:value={formData.proxy_trusted_proxies}
        on:input={handleChange}
        placeholder="e.g. 203.0.113.5/32 (your proxy's IP) or its docker subnet — never a whole private range"
        disabled={!formData.proxy_enabled}
      />
    </FormField>

    <FormField size="lg" id="proxy_shared_secret" label={$t('settings.proxy.sharedSecret')} hint={sharedSecretSet ? $t('settings.proxy.sharedSecretSetHelp') : $t('settings.proxy.sharedSecretHelp')} let:describedby>
      <div class="input-with-toggle">
        <input dir="ltr"
          id="proxy_shared_secret"
        aria-describedby={describedby}
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
    </FormField>
  </div>

  <div class="section" class:disabled={!formData.proxy_enabled}>
    <h3>{$t('settings.proxy.headerConfiguration')}</h3>

    <div class="form-row">
      <FormField size="lg" id="proxy_email_header" label={$t('settings.proxy.emailHeader')} hint={$t('settings.proxy.emailHeaderHelp')} let:describedby>
        <input dir="ltr"
          id="proxy_email_header" aria-describedby={describedby}
          type="text"
          bind:value={formData.proxy_email_header}
          on:input={handleChange}
          placeholder="X-Forwarded-Email"
          disabled={!formData.proxy_enabled}
        />
      </FormField>

      <FormField size="lg" id="proxy_name_header" label={$t('settings.proxy.nameHeader')} hint={$t('settings.proxy.nameHeaderHelp')} let:describedby>
        <input dir="ltr"
          id="proxy_name_header" aria-describedby={describedby}
          type="text"
          bind:value={formData.proxy_name_header}
          on:input={handleChange}
          placeholder="X-Forwarded-User"
          disabled={!formData.proxy_enabled}
        />
      </FormField>
    </div>

    <div class="form-row">
      <FormField size="lg" id="proxy_groups_header" label={$t('settings.proxy.groupsHeader')} hint={$t('settings.proxy.groupsHeaderHelp')} let:describedby>
        <input dir="ltr"
          id="proxy_groups_header" aria-describedby={describedby}
          type="text"
          bind:value={formData.proxy_groups_header}
          on:input={handleChange}
          placeholder="X-Forwarded-Groups"
          disabled={!formData.proxy_enabled}
        />
      </FormField>

      <FormField size="lg" id="proxy_groups_separator" label={$t('settings.proxy.groupsSeparator')} hint={$t('settings.proxy.groupsSeparatorHelp')} let:describedby>
        <input dir="ltr"
          id="proxy_groups_separator" aria-describedby={describedby}
          type="text"
          bind:value={formData.proxy_groups_separator}
          on:input={handleChange}
          placeholder=","
          disabled={!formData.proxy_enabled}
        />
      </FormField>
    </div>
  </div>

  <div class="section" class:disabled={!formData.proxy_enabled}>
    <h3>{$t('settings.proxy.authorization')}</h3>

    <FormField size="lg" id="proxy_role_header" label={$t('settings.proxy.roleHeader')} hint={$t('settings.proxy.roleHeaderHelp')} let:describedby>
      <input dir="ltr"
        id="proxy_role_header" aria-describedby={describedby}
        type="text"
        bind:value={formData.proxy_role_header}
        on:input={handleChange}
        placeholder="X-Forwarded-Role"
        disabled={!formData.proxy_enabled}
      />
    </FormField>

    {#if roleHeaderOff}
      <p class="ceiling-warning" role="status">
        {$t('settings.proxy.roleHeaderOffNotice')}
      </p>
    {/if}

    <FormField size="lg" id="proxy_allowed_domains" label={$t('settings.proxy.allowedDomains')} hint={$t('settings.proxy.allowedDomainsHelp')} let:describedby>
      <input dir="ltr"
        id="proxy_allowed_domains" aria-describedby={describedby}
        type="text"
        bind:value={formData.proxy_allowed_domains}
        on:input={handleChange}
        placeholder="example.com, example.org"
        disabled={!formData.proxy_enabled}
      />
    </FormField>

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

  .section :global(.form-field) {
    flex: 1;
    margin-bottom: var(--space-4);
  }

  .section :global(.form-field input[type="text"]),
  .section :global(.form-field input[type="password"]) {
    width: 100%;
  }

  .section :global(.form-field input:disabled) {
    cursor: not-allowed;
  }

  .input-with-toggle {
    display: flex;
    gap: var(--space-2);
  }

  .input-with-toggle input {
    flex: 1;
  }

  .help-text.indented {
    margin-inline-start: var(--space-6);
    margin-bottom: var(--space-3);
  }

  .ceiling-warning {
    margin: var(--space-2) 0 var(--space-4) 0;
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

  @media (max-width: 768px) {
    .form-row {
      flex-direction: column;
      gap: 0;
    }
  }
</style>
